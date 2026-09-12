#include "pp-samba.h"
#include "config.h"

#include <glib/gi18n.h>

struct _PpSamba
{
  PpHost parent_instance;
};

G_DEFINE_TYPE (PpSamba, pp_samba, PP_TYPE_HOST);

static void
pp_samba_class_init (PpSambaClass *klass)
{
}

static void
pp_samba_init (PpSamba *self)
{
}

PpSamba *
pp_samba_new (const gchar *hostname)
{
  return g_object_new (PP_TYPE_SAMBA,
                       "hostname", hostname,
                       NULL);
}

void
pp_samba_get_devices_async (PpSamba             *self,
                            gboolean             auth_if_needed,
                            GCancellable        *cancellable,
                            GAsyncReadyCallback  callback,
                            gpointer             user_data)
{
  GTask *task = g_task_new (G_OBJECT (self), cancellable, callback, user_data);
  g_task_return_pointer (task,
                         g_ptr_array_new_with_free_func (g_object_unref),
                         (GDestroyNotify) g_ptr_array_unref);
  g_object_unref (task);
}

GPtrArray *
pp_samba_get_devices_finish (PpSamba       *self,
                             GAsyncResult  *res,
                             GError       **error)
{
  g_return_val_if_fail (g_task_is_valid (res, self), NULL);

  return g_task_propagate_pointer (G_TASK (res), error);
}

void
pp_samba_set_auth_info (PpSamba     *self,
                        const gchar *username,
                        const gchar *password)
{
}
