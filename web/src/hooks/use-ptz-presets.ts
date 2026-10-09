import axios from "axios";
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { useSWRConfig } from "swr";

// Write operations on the camera's ONVIF presets and home position. Each call
// reports its own outcome as a toast and resolves to whether it succeeded.
export function usePtzPresetActions(camera: string) {
  const { t } = useTranslation(["views/live"]);
  const { mutate } = useSWRConfig();
  const [isSaving, setIsSaving] = useState(false);

  const run = useCallback(
    async (request: () => Promise<unknown>, successKey: string) => {
      setIsSaving(true);

      try {
        await request();
        toast.success(t(successKey), { position: "top-center" });
        await mutate(`${camera}/ptz/info`);
        return true;
      } catch (error) {
        // FastAPI validation errors carry a list of objects in detail, which
        // cannot be rendered, so only plain string messages are shown as-is
        const data = axios.isAxiosError(error)
          ? error.response?.data
          : undefined;
        const message = [data?.message, data?.detail].find(
          (value): value is string => typeof value === "string" && !!value,
        );
        toast.error(message ?? t("ptz.presetManagement.error"), {
          position: "top-center",
        });
        return false;
      } finally {
        setIsSaving(false);
      }
    },
    [camera, mutate, t],
  );

  const createPreset = useCallback(
    (name: string) =>
      run(
        () => axios.post(`${camera}/ptz/presets`, { name }),
        "ptz.presetManagement.success.create",
      ),
    [camera, run],
  );

  // ONVIF has no rename: overwriting always stores the current position
  const overwritePreset = useCallback(
    (token: string, name: string | null) =>
      run(
        () =>
          axios.put(`${camera}/ptz/presets/${encodeURIComponent(token)}`, {
            name,
          }),
        "ptz.presetManagement.success.overwrite",
      ),
    [camera, run],
  );

  const deletePreset = useCallback(
    (token: string) =>
      run(
        () =>
          axios.delete(`${camera}/ptz/presets/${encodeURIComponent(token)}`),
        "ptz.presetManagement.success.delete",
      ),
    [camera, run],
  );

  const setHome = useCallback(
    () => run(() => axios.post(`${camera}/ptz/home`), "ptz.home.setSuccess"),
    [camera, run],
  );

  return { createPreset, overwritePreset, deletePreset, setHome, isSaving };
}
