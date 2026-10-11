import { MutableRefObject, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuVideoOff } from "react-icons/lu";

type NoVideoBadgeProps = {
  // seconds without a new picture before the badge shows
  timeout: number;
  // when Frigate stopped receiving the camera's frames, if it has
  videoLossSince?: number;
  // whether the tile should be showing live video, and when it last showed a
  // new picture (0 until it has shown one)
  expectLive: boolean;
  lastPictureRef: MutableRefObject<number>;
  compact?: boolean;
};

export function NoVideoBadge({
  timeout,
  videoLossSince,
  expectLive,
  lastPictureRef,
  compact = false,
}: NoVideoBadgeProps) {
  const { t } = useTranslation(["components/player"]);
  const [elapsed, setElapsed] = useState<number>();

  // an idle tile's last still can be old, so going live starts the count over
  const liveSince = useRef(0);
  useEffect(() => {
    if (expectLive) {
      liveSince.current = Date.now();
    }
  }, [expectLive]);

  const watching = expectLive || videoLossSince !== undefined;

  useEffect(() => {
    if (!watching) {
      setElapsed(undefined);
      return;
    }

    const update = () => {
      // a tile that has not shown a picture yet is still connecting, and
      // has nothing to go stale
      const stale = expectLive && lastPictureRef.current > 0;
      const since = Math.min(
        videoLossSince ?? Infinity,
        stale ? Math.max(lastPictureRef.current, liveSince.current) : Infinity,
      );
      setElapsed(Date.now() - since);
    };

    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [watching, expectLive, videoLossSince, lastPictureRef]);

  if (elapsed === undefined || elapsed < timeout * 1000) {
    return null;
  }

  return (
    <div className="pointer-events-none absolute bottom-2 left-2 z-40 flex items-center gap-1.5 rounded-md bg-black/70 px-2 py-1 text-xs text-white">
      <LuVideoOff className="size-3.5 text-danger" />
      {!compact && <span>{t("noVideo")}</span>}
      <span className="tabular-nums">{formatElapsed(elapsed)}</span>
    </div>
  );
}

// m:ss, or h:mm:ss past an hour
function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");

  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
}
