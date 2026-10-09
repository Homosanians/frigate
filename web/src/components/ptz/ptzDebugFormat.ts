// A number from the PTZ log, or "-" when the camera or Frigate left it out
export function formatPtzNumber(value: unknown, digits: number = 3): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(digits)
    : "-";
}

// Entry times are seconds since the epoch, shown to the millisecond
export function formatPtzTime(seconds: number): string {
  const d = new Date(seconds * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  const ms = String(d.getMilliseconds()).padStart(3, "0");
  return `${hh}:${mm}:${ss}.${ms}`;
}
