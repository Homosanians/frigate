import { useContext, useEffect, useMemo, useState } from "react";
import { get as getData } from "idb-keyval";
import useSWR from "swr";
import { baseUrl } from "@/api/baseUrl";
import { AuthContext } from "@/context/auth-context";
import { getUserNamespacedKey } from "@/hooks/use-user-persistence";
import { CameraConfig, FrigateConfig } from "@/types/frigateConfig";
import {
  isRestreamedStream,
  isTranscodeStreamName,
} from "@/utils/liveTranscode";
import { supportedMseCodecs } from "@/utils/mseUtil";

// how long to wait before asking again once the socket drops
const RETRY_MS = 15000;

/**
 * Keeps the live cache connected to the stream each camera's page would
 * open, so the page starts from cached frames. Only while the tab is
 * visible, because the cameras send to Frigate for as long as this holds.
 */
export function useLiveCacheWarmup(cameras: CameraConfig[]) {
  const { data: config } = useSWR<FrigateConfig>("config");
  // a tab can open hidden, as a restored session's background tabs do
  const [visible, setVisible] = useState(
    document.visibilityState === "visible",
  );

  useEffect(() => {
    const update = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", update);

    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  const { auth } = useContext(AuthContext);
  const username = auth?.user?.username;
  const [streams, setStreams] = useState<string[]>([]);

  const cached = useMemo(
    () => cameras.filter((camera) => camera.live.cache.enabled),
    [cameras],
  );

  useEffect(() => {
    let cancelled = false;

    Promise.all(
      cached.map(async (camera) => {
        const available = Object.values(camera.live.streams);
        // the camera page opens a pinned stream, and the first one otherwise
        const pinned = await getData<string>(
          getUserNamespacedKey(`${camera.name}-stream`, username),
        ).catch(() => undefined);

        const stream =
          pinned && available.includes(pinned) ? pinned : available[0];

        // transcoding only runs while someone watches
        return stream && !isTranscodeStreamName(camera.name, stream)
          ? stream
          : undefined;
      }),
    ).then((names) => {
      if (cancelled) {
        return;
      }

      const next = [...new Set(names)]
        .filter(
          (name): name is string => !!name && isRestreamedStream(config, name),
        )
        .sort();

      // the same list must not render the dashboard again
      setStreams((previous) =>
        previous.join("\n") === next.join("\n") ? previous : next,
      );
    });

    return () => {
      cancelled = true;
    };
  }, [cached, username, config]);

  const key = streams.join("\n");

  useEffect(() => {
    if (
      !visible ||
      !key ||
      !("MediaSource" in window || "ManagedMediaSource" in window)
    ) {
      return;
    }

    const query = key
      .split("\n")
      .map((stream) => `src=${encodeURIComponent(stream)}`)
      .join("&");
    let ws: WebSocket | undefined;
    let retry: number | undefined;
    let done = false;

    const connect = () => {
      ws = new WebSocket(
        `${baseUrl.replace(/^http/, "ws")}live/cache/warm?${query}`,
      );
      ws.addEventListener("open", () =>
        ws?.send(JSON.stringify({ type: "warm", value: supportedMseCodecs() })),
      );
      ws.addEventListener("close", () => {
        if (!done) {
          retry = window.setTimeout(connect, RETRY_MS);
        }
      });
    };

    connect();

    return () => {
      done = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [visible, key]);
}
