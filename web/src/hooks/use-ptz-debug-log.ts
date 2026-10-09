import axios from "axios";
import { useCallback, useEffect, useRef, useState } from "react";
import { PtzDebugResponse, PtzDebugRowItem } from "@/types/ptz";

// how often the open log asks Frigate for new entries
const POLL_INTERVAL_MS = 1000;
// rows kept on screen
const MAX_ROWS = 1000;

function mergeRows(
  rows: PtzDebugRowItem[],
  additions: PtzDebugRowItem[],
): PtzDebugRowItem[] {
  const merged = [...rows];
  const index = new Map(merged.map((row, i) => [row.key, i]));

  for (const row of additions) {
    const at = index.get(row.key);

    if (at === undefined) {
      index.set(row.key, merged.length);
      merged.push(row);
    } else {
      merged[at] = row;
    }
  }

  // a request is logged when the camera answers, after later entries
  merged.sort((a, b) => a.time - b.time);
  return merged.length > MAX_ROWS
    ? merged.slice(merged.length - MAX_ROWS)
    : merged;
}

type PtzDebugLog = {
  rows: PtzDebugRowItem[];
  response: PtzDebugResponse | undefined;
  failed: boolean;
  clear: () => void;
};

// Polls a camera's PTZ log while enabled and the page is visible. Frigate
// records the log only while it is polled, so it stops shortly after this does.
export function usePtzDebugLog(
  camera: string,
  enabled: boolean,
  paused: boolean,
): PtzDebugLog {
  const [rows, setRows] = useState<PtzDebugRowItem[]>([]);
  const [response, setResponse] = useState<PtzDebugResponse>();
  const [failed, setFailed] = useState(false);
  const afterRef = useRef(0);
  const sessionRef = useRef<string | null>(null);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => {
    // another camera starts a new log
    afterRef.current = 0;
    sessionRef.current = null;
    setRows([]);
    setResponse(undefined);
    setFailed(false);
  }, [camera]);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    let cancelled = false;
    let polling = false;

    const poll = async () => {
      if (polling || document.visibilityState !== "visible") {
        return;
      }

      polling = true;

      try {
        const { data } = await axios.get<PtzDebugResponse>(
          `${camera}/ptz/debug`,
          { params: { after: afterRef.current } },
        );

        if (cancelled) {
          return;
        }

        const additions: PtzDebugRowItem[] = [];
        const markerTime =
          data.entries.length > 0
            ? Math.min(...data.entries.map((entry) => entry.time)) - 0.001
            : Date.now() / 1000;

        if (
          sessionRef.current !== null &&
          sessionRef.current !== data.session
        ) {
          // Frigate restarted and its entry numbers started over
          additions.push({
            kind: "marker",
            key: `restart-${data.session}`,
            time: markerTime,
            reason: "restart",
          });
          afterRef.current = 0;
        } else {
          if (data.missed) {
            additions.push({
              kind: "marker",
              key: `missed-${data.session}-${data.seq}`,
              time: markerTime,
              reason: "missed",
            });
          }

          additions.push(
            ...data.entries.map((entry) => ({
              ...entry,
              key: `${data.session}-${entry.id}`,
            })),
          );
          afterRef.current = data.seq;
        }

        sessionRef.current = data.session;
        setResponse(data);
        setFailed(false);

        if (!pausedRef.current && additions.length > 0) {
          setRows((current) => mergeRows(current, additions));
        }
      } catch {
        if (!cancelled) {
          setFailed(true);
        }
      } finally {
        polling = false;
      }
    };

    poll();
    const timer = setInterval(poll, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [camera, enabled]);

  const clear = useCallback(() => setRows([]), []);

  return { rows, response, failed, clear };
}
