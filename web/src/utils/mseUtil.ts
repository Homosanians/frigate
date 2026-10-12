// codecs offered to go2rtc for MSE playback
export const MSE_CODECS: string[] = [
  "avc1.640029", // H.264 high 4.1 (Chromecast 1st and 2nd Gen)
  "avc1.64002A", // H.264 high 4.2 (Chromecast 3rd Gen)
  "avc1.640033", // H.264 high 5.1 (Chromecast with Google TV)
  "hvc1.1.6.L153.B0", // H.265 main 5.1 (Chromecast Ultra)
  "mp4a.40.2", // AAC LC
  "mp4a.40.5", // AAC HE
  "flac", // FLAC (PCM compatible)
  "opus", // OPUS Chrome, Firefox
];

/**
 * The codec list this browser sends for a stream. Every caller gets the same
 * string, which is what lets live cache connections be shared.
 */
export function supportedMseCodecs(): string {
  // safari has the managed kind, which codecSupport.ts declares on window
  const source = window.ManagedMediaSource ?? window.MediaSource;

  return MSE_CODECS.filter((codec) =>
    source.isTypeSupported(`video/mp4; codecs="${codec}"`),
  ).join();
}
