import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import copy from "copy-to-clipboard";
import { toast } from "sonner";
import { FaCopy, FaEraser, FaPause, FaPlay } from "react-icons/fa";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { usePtzDebugLog } from "@/hooks/use-ptz-debug-log";
import { PtzDebugResponse, PtzPosition, PtzStatus } from "@/types/ptz";
import PtzDebugRow from "./PtzDebugRow";
import { ptzLogText } from "./ptzDebugCopy";
import { formatPtzNumber, formatPtzTime } from "./ptzDebugFormat";
import { summarizePtzSpaces } from "./ptzDebugSummary";

function StatusLine({ status }: { status: PtzStatus | null }) {
  const { t } = useTranslation(["views/settings"]);

  if (!status) {
    return (
      <div className="text-muted-foreground">
        {t("debug.ptz.status.unknown")}
      </div>
    );
  }

  if (status.error) {
    return (
      <div className="text-destructive">
        {status.error === "timeout"
          ? t("debug.ptz.status.timeout")
          : t("debug.ptz.status.failed", { error: status.error })}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span
        className={cn(
          "rounded border px-1.5 py-0.5 font-mono",
          status.pan_tilt === "MOVING"
            ? "border-amber-500/30 bg-amber-500/20 text-amber-700 dark:text-amber-300"
            : "border-green-500/30 bg-green-500/20 text-green-700 dark:text-green-300",
        )}
      >
        {status.pan_tilt ?? t("debug.ptz.status.noMoveStatus")}
      </span>
      <span className="font-mono">
        {status.position
          ? t("debug.ptz.status.position", {
              pan: formatPtzNumber(status.position.pan),
              tilt: formatPtzNumber(status.position.tilt),
              zoom: formatPtzNumber(status.position.zoom),
            })
          : t("debug.ptz.status.noPosition")}
      </span>
      <span className="text-muted-foreground">
        {t("debug.ptz.status.read", { time: formatPtzTime(status.time) })}
      </span>
    </div>
  );
}

function PtzDebugHeader({
  response,
  failed,
}: {
  response: PtzDebugResponse | undefined;
  failed: boolean;
}) {
  const { t } = useTranslation(["views/settings"]);

  if (!response) {
    return (
      <p className="text-xs text-muted-foreground">
        {failed ? t("debug.ptz.pollFailed") : t("debug.ptz.loading")}
      </p>
    );
  }

  if (!response.connected) {
    return (
      <p className="text-xs text-muted-foreground">
        {t("debug.ptz.notConnected")}
      </p>
    );
  }

  const spaces = response.capabilities?.relative_spaces ?? [];
  const defaultSpace = response.capabilities?.default_relative_space;

  return (
    <div
      className="flex flex-col gap-1 rounded-md border border-secondary p-2 text-xs"
      data-testid="ptz-debug-status"
    >
      <StatusLine status={response.status} />
      {spaces.length > 0 && (
        <div className="text-muted-foreground">
          {summarizePtzSpaces(spaces, t)}
        </div>
      )}
      {defaultSpace && (
        <div className="text-muted-foreground">
          {t("debug.ptz.defaultSpace", { space: defaultSpace })}
        </div>
      )}
      {response.capabilities?.relative_mode && (
        <div className="text-muted-foreground">
          {t("debug.ptz.relativeMode", {
            mode: response.capabilities.relative_mode,
          })}
        </div>
      )}
      {failed && (
        <div className="text-destructive">{t("debug.ptz.pollFailed")}</div>
      )}
    </div>
  );
}

type PtzDebugLogProps = {
  camera: string;
  // the log polls the camera only while its tab is shown
  active: boolean;
};

export default function PtzDebugLog({ camera, active }: PtzDebugLogProps) {
  const { t } = useTranslation(["views/settings"]);
  const [paused, setPaused] = useState(false);
  const { rows, response, failed, clear } = usePtzDebugLog(
    camera,
    active,
    paused,
  );

  const scrollRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;

    if (el) {
      followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    }
  }, []);

  useEffect(() => {
    const el = scrollRef.current;

    if (el && followRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [rows]);

  // each status row shows how far the camera moved since it last stood still
  const previousPositions = useMemo(() => {
    const previous = new Map<string, PtzPosition | null>();
    let idleAt: PtzPosition | null = null;

    for (const row of rows) {
      if (row.kind !== "status") {
        continue;
      }

      previous.set(row.key, idleAt);
      const position = row.data.position as PtzPosition | null;

      // standing still means no axis moves, and some cameras report no zoom
      // status at all
      const standingStill =
        row.data.pan_tilt === "IDLE" &&
        (row.data.zoom == null || row.data.zoom === "IDLE");

      if (standingStill && position) {
        idleAt = position;
      }
    }

    return previous;
  }, [rows]);

  const entryCount = rows.filter((row) => row.kind !== "marker").length;

  const handleCopy = useCallback(async () => {
    if (await copy(ptzLogText(camera, response, rows, previousPositions, t))) {
      toast.success(t("debug.ptz.copySuccess"));
    } else {
      toast.error(t("debug.ptz.copyError"));
    }
  }, [camera, response, rows, previousPositions, t]);

  return (
    <div className="flex w-full flex-col gap-3">
      <p className="text-xs text-muted-foreground">{t("debug.ptz.desc")}</p>
      <PtzDebugHeader response={response} failed={failed} />
      <div className="flex flex-row items-center justify-between gap-2">
        <Badge variant="secondary" className="text-xs text-primary-variant">
          {t("debug.ptz.count", { count: entryCount })}
        </Badge>
        <div className="flex items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1 px-2 text-xs"
            onClick={() => setPaused(!paused)}
            aria-label={paused ? t("debug.ptz.resume") : t("debug.ptz.pause")}
          >
            {paused ? (
              <FaPlay className="size-2.5" />
            ) : (
              <FaPause className="size-2.5" />
            )}
            {paused ? t("debug.ptz.resume") : t("debug.ptz.pause")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1 px-2 text-xs"
            onClick={clear}
            aria-label={t("debug.ptz.clear")}
          >
            <FaEraser className="size-2.5" />
            {t("debug.ptz.clear")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1 px-2 text-xs"
            onClick={handleCopy}
            disabled={!response && rows.length === 0}
            aria-label={t("debug.ptz.copy")}
          >
            <FaCopy className="size-2.5" />
            {t("debug.ptz.copy")}
          </Button>
        </div>
      </div>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="scrollbar-container h-[50dvh] overflow-y-auto rounded-md border border-secondary"
      >
        {rows.length === 0 ? (
          <div className="flex size-full items-center justify-center p-4 text-xs text-muted-foreground">
            {t("debug.ptz.empty")}
          </div>
        ) : (
          rows.map((row) => (
            <PtzDebugRow
              key={row.key}
              row={row}
              previousPosition={previousPositions.get(row.key) ?? null}
            />
          ))
        )}
      </div>
    </div>
  );
}
