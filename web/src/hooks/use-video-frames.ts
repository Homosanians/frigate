import { RefObject, useEffect } from "react";

const CHECK_INTERVAL_MS = 1000;

/**
 * Call onFrame whenever the video has decoded new frames since the last check.
 * This counts decoded frames rather than painted ones: a browser stops painting
 * a window that sits behind another, but keeps decoding its video.
 */
export function useVideoFrames(
  videoRef: RefObject<HTMLVideoElement | null>,
  onFrame?: () => void,
) {
  useEffect(() => {
    const video = videoRef.current;

    if (!video || !onFrame) {
      return;
    }

    // playback progress stands in where decode counts are unavailable
    const progress = () =>
      typeof video.getVideoPlaybackQuality === "function"
        ? video.getVideoPlaybackQuality().totalVideoFrames
        : video.currentTime;

    let last = progress();
    const interval = setInterval(() => {
      const current = progress();

      // a new source starts the count over, which is not a new frame
      if (current > last) {
        onFrame();
      }

      last = current;
    }, CHECK_INTERVAL_MS);

    return () => clearInterval(interval);
  }, [videoRef, onFrame]);
}
