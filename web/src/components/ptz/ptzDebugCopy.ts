import type { TFunction } from "i18next";
import { PtzDebugResponse, PtzDebugRowItem, PtzPosition } from "@/types/ptz";
import { formatPtzTime } from "./ptzDebugFormat";
import {
  summarizePtzRow,
  summarizePtzSpaces,
  summarizePtzStatus,
} from "./ptzDebugSummary";

// The PTZ log as plain text: the camera and its status first, then each row
// as it reads on screen followed by the row's full data, so it can be read
// as is or pasted into an issue without losing anything
export function ptzLogText(
  camera: string,
  response: PtzDebugResponse | undefined,
  rows: PtzDebugRowItem[],
  previousPositions: Map<string, PtzPosition | null>,
  t: TFunction,
): string {
  const lines = [camera];

  if (response && !response.connected) {
    lines.push(t("debug.ptz.notConnected", { ns: "views/settings" }));
  } else if (response) {
    lines.push(summarizePtzStatus(response.status, t));
    const capabilities = response.capabilities;

    if (capabilities && capabilities.relative_spaces.length > 0) {
      lines.push(summarizePtzSpaces(capabilities.relative_spaces, t));
    }

    if (capabilities?.default_relative_space) {
      lines.push(
        t("debug.ptz.defaultSpace", {
          ns: "views/settings",
          space: capabilities.default_relative_space,
        }),
      );
    }
  }

  lines.push("");

  for (const row of rows) {
    const summary = summarizePtzRow(
      row,
      previousPositions.get(row.key) ?? null,
      t,
    );

    if (row.kind === "marker") {
      lines.push(`--- ${summary} ---`);
      continue;
    }

    lines.push(`${formatPtzTime(row.time)} [${row.source}] ${summary}`);
    lines.push(`  ${JSON.stringify(row.data)}`);
  }

  return lines.join("\n");
}
