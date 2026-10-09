import type { TFunction } from "i18next";
import { PtzDebugEntryRow, PtzDebugRowItem, PtzPosition } from "@/types/ptz";
import { formatPtzNumber as n } from "./ptzDebugFormat";

// helpers that get t from a component name the namespace in every call, so
// the i18n extractor files the keys under views/settings
type Data = Record<string, unknown>;

const REFUSAL_KEYS: Record<string, string> = {
  busy: "debug.ptz.reason.busy",
  unsupported: "debug.ptz.reason.unsupported",
  unknown_preset: "debug.ptz.reason.unknown_preset",
  not_connected: "debug.ptz.reason.not_connected",
};

const SKIP_KEYS: Record<string, string> = {
  centered: "debug.ptz.skip.centered",
  stale_frame: "debug.ptz.skip.stale_frame",
  queue_busy: "debug.ptz.skip.queue_busy",
};

const OBJECT_KEYS: Record<string, string> = {
  new: "debug.ptz.summary.objectNew",
  reacquired: "debug.ptz.summary.objectReacquired",
  ended: "debug.ptz.summary.objectEnded",
};

function asPosition(value: unknown): PtzPosition | null {
  return value && typeof value === "object" ? (value as PtzPosition) : null;
}

function change(to: number | null, from: number | null): string {
  if (to === null || from === null) {
    return "-";
  }

  const d = to - from;
  return `${d >= 0 ? "+" : ""}${d.toFixed(3)}`;
}

function reason(
  keys: Record<string, string>,
  value: unknown,
  t: TFunction,
): string {
  return t(keys[String(value)] ?? "debug.ptz.reason.unknown", {
    ns: "views/settings",
  });
}

function summarizeRequest(data: Data, t: TFunction): string {
  const operation = String(data.operation);
  let text: string;

  if (data.service === "imaging") {
    text = t("debug.ptz.summary.focus", {
      ns: "views/settings",
      operation,
      speed: n(data.focus, 1),
    });
  } else if (operation === "RelativeMove") {
    text = t("debug.ptz.summary.relativeMove", {
      ns: "views/settings",
      operation,
      space: data.space,
      pan: n(data.pan),
      tilt: n(data.tilt),
      x: n(data.x),
      y: n(data.y),
    });
  } else if (operation === "ContinuousMove") {
    text = t("debug.ptz.summary.continuousMove", {
      ns: "views/settings",
      operation,
      pan: n(data.pan, 1),
      tilt: n(data.tilt, 1),
      zoom: n(data.zoom, 1),
    });
  } else if (operation === "AbsoluteMove") {
    text = t("debug.ptz.summary.absoluteMove", {
      ns: "views/settings",
      operation,
      zoom: n(data.zoom),
      sent: n(data.sent),
    });
  } else if (data.preset !== undefined) {
    text = t("debug.ptz.summary.preset", {
      ns: "views/settings",
      operation,
      preset: data.preset,
    });
  } else {
    text = operation;
  }

  const outcome = data.error
    ? t("debug.ptz.summary.failed", { ns: "views/settings", error: data.error })
    : t("debug.ptz.summary.duration", {
        ns: "views/settings",
        duration: data.duration_ms,
      });
  return t("debug.ptz.summary.withOutcome", {
    ns: "views/settings",
    text,
    outcome,
  });
}

function summarizeStatus(
  data: Data,
  previous: PtzPosition | null,
  t: TFunction,
): string {
  if (data.error) {
    return data.error === "timeout"
      ? t("debug.ptz.summary.statusTimeout", { ns: "views/settings" })
      : t("debug.ptz.summary.statusFailed", {
          ns: "views/settings",
          error: data.error,
        });
  }

  const status = data.pan_tilt ?? "-";
  const position = asPosition(data.position);

  if (!position) {
    return t("debug.ptz.summary.statusNoPosition", {
      ns: "views/settings",
      status,
    });
  }

  const text = t("debug.ptz.summary.status", {
    ns: "views/settings",
    status,
    pan: n(position.pan),
    tilt: n(position.tilt),
  });

  if (
    previous &&
    (previous.pan !== position.pan || previous.tilt !== position.tilt)
  ) {
    return t("debug.ptz.summary.moved", {
      ns: "views/settings",
      text,
      pan: change(position.pan, previous.pan),
      tilt: change(position.tilt, previous.tilt),
    });
  }

  return text;
}

function summarizeAutotrack(data: Data, t: TFunction): string {
  switch (data.event) {
    case "object":
      return t(OBJECT_KEYS[String(data.action)] ?? OBJECT_KEYS.new, {
        ns: "views/settings",
        label: data.label,
        id: data.id,
      });
    case "decision": {
      const centroid = (data.centroid as number[] | undefined) ?? [];
      const text = t("debug.ptz.summary.decision", {
        ns: "views/settings",
        x: centroid[0],
        y: centroid[1],
        pan: n(data.pan),
        tilt: n(data.tilt),
        zoom: n(data.zoom),
      });
      return data.predicted
        ? t("debug.ptz.summary.predicted", {
            ns: "views/settings",
            text,
            seconds: n(data.move_time, 2),
          })
        : text;
    }
    case "skipped":
      return t("debug.ptz.summary.skipped", {
        ns: "views/settings",
        reason: reason(SKIP_KEYS, data.reason, t),
      });
    case "move_done":
      if (!data.stopped) {
        return t("debug.ptz.summary.moveNotStopped", { ns: "views/settings" });
      }
      return data.predicted_time === null
        ? t("debug.ptz.summary.moveDone", {
            ns: "views/settings",
            actual: n(data.actual_time, 2),
          })
        : t("debug.ptz.summary.moveDonePredicted", {
            ns: "views/settings",
            actual: n(data.actual_time, 2),
            predicted: n(data.predicted_time, 2),
          });
    case "video_settled":
      return t("debug.ptz.summary.videoSettled", {
        ns: "views/settings",
        after: n(data.after_stop, 2),
        latency: n(data.stream_latency, 2),
      });
    case "return_to_preset":
      return t("debug.ptz.summary.returnToPreset", {
        ns: "views/settings",
        preset: data.preset,
        seconds: n(data.idle_for, 1),
      });
    default:
      return String(data.event);
  }
}

function summarizeCalibration(data: Data, t: TFunction): string {
  switch (data.event) {
    case "started":
      return t("debug.ptz.summary.calibrationStarted", {
        ns: "views/settings",
        steps: data.steps,
      });
    case "progress":
      return t("debug.ptz.summary.calibrationProgress", {
        ns: "views/settings",
        percent: data.percent,
      });
    case "finished":
      return t("debug.ptz.summary.calibrationFinished", {
        ns: "views/settings",
        latency: n(data.stream_latency, 2),
      });
    default:
      if (data.reason === "busy") {
        return t("debug.ptz.summary.calibrationBusy", { ns: "views/settings" });
      }

      if (data.reason === "invalid_coefficients") {
        return t("debug.ptz.summary.calibrationInvalid", {
          ns: "views/settings",
        });
      }

      return t("debug.ptz.summary.calibrationFailed", {
        ns: "views/settings",
        reason: data.reason,
      });
  }
}

function summarizeEntry(
  row: PtzDebugEntryRow,
  previous: PtzPosition | null,
  t: TFunction,
): string {
  const data = row.data;

  switch (row.kind) {
    case "request":
      return summarizeRequest(data, t);
    case "refused":
      return t("debug.ptz.summary.refused", {
        ns: "views/settings",
        operation: data.operation,
        reason: reason(REFUSAL_KEYS, data.reason, t),
      });
    case "status":
      return summarizeStatus(data, previous, t);
    case "external_move": {
      const from = asPosition(data.from);
      const to = asPosition(data.to);
      return t("debug.ptz.summary.externalMove", {
        ns: "views/settings",
        fromPan: n(from?.pan),
        toPan: n(to?.pan),
        fromTilt: n(from?.tilt),
        toTilt: n(to?.tilt),
      });
    }
    case "autotrack":
      return summarizeAutotrack(data, t);
    case "calibration":
      return summarizeCalibration(data, t);
    case "connection":
      return data.connected
        ? t("debug.ptz.summary.connected", {
            ns: "views/settings",
            features: ((data.features as string[] | undefined) ?? []).join(
              ", ",
            ),
          })
        : t("debug.ptz.summary.connectionFailed", { ns: "views/settings" });
  }
}

// One line for a row of the PTZ log. previous is the camera position at the
// last IDLE status before this row, to show how far a move went.
export function summarizePtzRow(
  row: PtzDebugRowItem,
  previous: PtzPosition | null,
  t: TFunction,
): string {
  if (row.kind === "marker") {
    return row.reason === "restart"
      ? t("debug.ptz.restart", { ns: "views/settings" })
      : t("debug.ptz.missed", { ns: "views/settings" });
  }

  const text = summarizeEntry(row, previous, t);

  if (row.repeats > 1 && row.until !== null) {
    return t("debug.ptz.summary.repeats", {
      ns: "views/settings",
      text,
      count: row.repeats,
      seconds: (row.until - row.time).toFixed(1),
    });
  }

  return text;
}
