#!/bin/sh

case "${TERM:-}" in
""|dumb|unknown)
  if [ -t 0 ]; then
    export TERM=linux
  fi
  ;;
esac
