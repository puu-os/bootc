# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from dataclasses import dataclass
from enum import Enum, auto
import http.client
import subprocess

import oksa_model_serving as ms

_GATEWAY_ERRORS = (OSError, http.client.HTTPException, subprocess.SubprocessError, ValueError)


class UserDeletionOutcome(Enum):
    INVALID = auto()
    PROTECTED = auto()
    NOT_STARTED = auto()
    KEY_REVOCATION_UNCONFIRMED = auto()
    USER_DELETION_UNCONFIRMED = auto()
    DELETED = auto()


@dataclass(frozen=True)
class UserDeletionResult:
    outcome: UserDeletionOutcome
    message: str
    confirmed_revoked_keys: int = 0

    @property
    def deleted(self) -> bool | None:
        if self.outcome is UserDeletionOutcome.DELETED:
            return True
        if self.outcome is UserDeletionOutcome.USER_DELETION_UNCONFIRMED:
            return None
        return False

    @property
    def uncertain(self) -> bool:
        return self.outcome in (
            UserDeletionOutcome.KEY_REVOCATION_UNCONFIRMED,
            UserDeletionOutcome.USER_DELETION_UNCONFIRMED,
        )


class UserDeletionService:
    def __init__(self, master_key: str | None = None) -> None:
        self._master_key = master_key

    def delete(self, user_id: str) -> UserDeletionResult:
        if not isinstance(user_id, str) or not user_id.strip():
            return UserDeletionResult(UserDeletionOutcome.INVALID, "User ID is required; deletion was not started")
        try:
            ok, keys = ms.litellm_key_list(user_id=user_id, master_key=self._master_key, include_service=True)
        except _GATEWAY_ERRORS as exc:
            return UserDeletionResult(
                UserDeletionOutcome.NOT_STARTED,
                f"Deletion of user {user_id} was not started: {type(exc).__name__}: {exc}",
            )
        if not ok:
            return UserDeletionResult(UserDeletionOutcome.NOT_STARTED, f"Deletion of user {user_id} was not started: {keys}")
        tokens = self._tokens(keys, user_id)
        if tokens is None:
            return UserDeletionResult(UserDeletionOutcome.NOT_STARTED, "Invalid LiteLLM key-list response; deletion was not started")
        if any(key.get("key_alias") == "open-webui" for key in keys):
            return UserDeletionResult(
                UserDeletionOutcome.PROTECTED,
                f"User {user_id} owns the Open WebUI service key and cannot be deleted; deletion was not started",
            )
        confirmed = 0
        if tokens:
            try:
                status, response = ms.litellm_api_call(
                    "POST", "/key/delete", payload={"keys": tokens}, master_key=self._master_key,
                )
            except _GATEWAY_ERRORS as exc:
                return self._key_unconfirmed(user_id, f"{type(exc).__name__}: {exc}")
            if status != 200:
                return self._key_unconfirmed(user_id, self._error_detail(response, status))
            acknowledged = response.get("deleted_keys") if isinstance(response, dict) else None
            if (not isinstance(acknowledged, list) or not all(isinstance(token, str) for token in acknowledged)
                    or len(acknowledged) != len(tokens) or set(acknowledged) != set(tokens)):
                return self._key_unconfirmed(user_id, "Invalid LiteLLM key-deletion acknowledgement")
            try:
                ok, remaining = ms.litellm_key_list(user_id=user_id, master_key=self._master_key, include_service=True)
            except _GATEWAY_ERRORS as exc:
                return self._key_unconfirmed(user_id, f"{type(exc).__name__}: {exc}")
            if not ok:
                return self._key_unconfirmed(user_id, f"Could not verify remaining keys: {remaining}")
            remaining_tokens = self._tokens(remaining, user_id)
            if remaining_tokens is None:
                return self._key_unconfirmed(user_id, "Invalid refreshed LiteLLM key-list response")
            remaining_ids = set(remaining_tokens)
            for token in tokens:
                if token in remaining_ids:
                    continue
                absent, detail = ms.litellm_key_absent(token, master_key=self._master_key)
                if not absent:
                    return self._key_unconfirmed(user_id, detail, confirmed)
                confirmed += 1
            if remaining_tokens:
                return self._key_unconfirmed(user_id, "API keys remain in the gateway catalog", confirmed)
        try:
            status, response = ms.litellm_api_call(
                "POST", "/user/delete", payload={"user_ids": [user_id]}, master_key=self._master_key,
            )
        except _GATEWAY_ERRORS as exc:
            return self._user_unconfirmed(user_id, f"{type(exc).__name__}: {exc}", confirmed)
        if status != 200:
            return self._user_unconfirmed(user_id, self._error_detail(response, status), confirmed)
        count = response.get("data") if isinstance(response, dict) else None
        if type(count) is not int or count != 1:
            return self._user_unconfirmed(user_id, "Invalid LiteLLM user-deletion acknowledgement", confirmed)
        return UserDeletionResult(UserDeletionOutcome.DELETED, f"User {user_id} deleted", confirmed)

    @staticmethod
    def _tokens(keys: object, user_id: str) -> list[str] | None:
        if not isinstance(keys, list):
            return None
        tokens = []
        for key in keys:
            if (not isinstance(key, dict) or key.get("user_id") != user_id
                    or not isinstance(key.get("token"), str) or not key["token"].strip()
                    or (key.get("key_alias") is not None and not isinstance(key["key_alias"], str))):
                return None
            tokens.append(key["token"])
        return list(dict.fromkeys(tokens))

    @staticmethod
    def _error_detail(response: object, status: int) -> str:
        if isinstance(response, dict):
            return str(response.get("error") or response.get("message") or f"HTTP {status}")
        return f"HTTP {status}"

    @staticmethod
    def _key_unconfirmed(user_id: str, detail: str, confirmed: int = 0) -> UserDeletionResult:
        summary = f"{confirmed} API key(s) were confirmed revoked. " if confirmed else ""
        return UserDeletionResult(
            UserDeletionOutcome.KEY_REVOCATION_UNCONFIRMED,
            f"User {user_id}: {summary}API key revocation was not fully confirmed: {detail}. "
            "User deletion was not attempted. Refresh before retrying.",
            confirmed,
        )

    @staticmethod
    def _user_unconfirmed(user_id: str, detail: str, confirmed: int) -> UserDeletionResult:
        summary = f"{confirmed} API key(s) were confirmed revoked. " if confirmed else ""
        return UserDeletionResult(
            UserDeletionOutcome.USER_DELETION_UNCONFIRMED,
            f"User {user_id}: {summary}User deletion was not confirmed: {detail}. Refresh before retrying.",
            confirmed,
        )
