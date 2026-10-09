import { memo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { PtzDebugRowItem, PtzPosition } from "@/types/ptz";
import { formatPtzTime } from "./ptzDebugFormat";
import { summarizePtzRow } from "./ptzDebugSummary";

const SOURCE_COLORS: Record<string, string> = {
  command: "bg-blue-500/20 text-blue-700 dark:text-blue-300 border-blue-500/30",
  api: "bg-purple-500/20 text-purple-700 dark:text-purple-300 border-purple-500/30",
  autotrack:
    "bg-green-500/20 text-green-700 dark:text-green-300 border-green-500/30",
  calibration:
    "bg-amber-500/20 text-amber-700 dark:text-amber-300 border-amber-500/30",
  debug: "bg-gray-500/20 text-gray-700 dark:text-gray-300 border-gray-500/30",
  frigate: "bg-cyan-500/20 text-cyan-700 dark:text-cyan-300 border-cyan-500/30",
};

const SOURCE_KEYS: Record<string, string> = {
  command: "debug.ptz.source.command",
  api: "debug.ptz.source.api",
  autotrack: "debug.ptz.source.autotrack",
  calibration: "debug.ptz.source.calibration",
  debug: "debug.ptz.source.debug",
  frigate: "debug.ptz.source.frigate",
};

type PtzDebugRowProps = {
  row: PtzDebugRowItem;
  previousPosition: PtzPosition | null;
};

const PtzDebugRow = memo(function PtzDebugRow({
  row,
  previousPosition,
}: PtzDebugRowProps) {
  const { t } = useTranslation(["views/settings"]);
  const [expanded, setExpanded] = useState(false);

  if (row.kind === "marker") {
    return (
      <div className="border-b border-secondary/50 px-2 py-1 text-center text-xs text-muted-foreground">
        {summarizePtzRow(row, null, t)}
      </div>
    );
  }

  // things that went wrong, or that Frigate did not do, stand out
  const alert =
    row.kind === "refused" ||
    row.kind === "external_move" ||
    (row.kind === "calibration" && row.data.event === "failed") ||
    Boolean(row.data.error) ||
    (row.kind === "connection" && !row.data.connected);

  return (
    <div className="border-b border-secondary/50" data-testid="ptz-debug-row">
      <div
        className={cn(
          "flex cursor-pointer items-start gap-2 px-2 py-1.5 transition-colors hover:bg-muted/50",
          expanded && "bg-muted/30",
        )}
        onClick={() => setExpanded((value) => !value)}
      >
        <ChevronRight
          className={cn(
            "mt-0.5 size-3.5 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-90",
          )}
        />
        <span className="shrink-0 font-mono text-xs text-muted-foreground">
          {formatPtzTime(row.time)}
        </span>
        <span
          className={cn(
            "shrink-0 rounded border px-1.5 py-0.5 text-xs",
            SOURCE_COLORS[row.source],
          )}
        >
          {t(SOURCE_KEYS[row.source] ?? "debug.ptz.source.frigate")}
        </span>
        <span
          className={cn(
            "min-w-0 break-words text-xs",
            alert ? "text-destructive" : "text-primary-variant",
          )}
        >
          {summarizePtzRow(row, previousPosition, t)}
        </span>
      </div>
      {expanded && (
        <pre className="overflow-x-auto border-t border-secondary/30 bg-background_alt/50 px-4 py-2 font-mono text-xs">
          {JSON.stringify(row.data, null, 2)}
        </pre>
      )}
    </div>
  );
});

export default PtzDebugRow;
