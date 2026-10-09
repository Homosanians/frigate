import WebRtcPlayer from "./WebRTCPlayer";
import { BirdseyeConfig } from "@/types/frigateConfig";
import ActivityIndicator from "../indicators/activity-indicator";
import JSMpegPlayer from "./JSMpegPlayer";
import MSEPlayer from "./MsePlayer";
import { LivePlayerMode, LiveVideoFit } from "@/types/live";
import { VIDEO_FIT_CLASS } from "@/utils/videoUtil";
import { cn } from "@/lib/utils";
import React from "react";
import { ImageShadowOverlay } from "../overlay/ImageShadowOverlay";

type LivePlayerProps = {
  className?: string;
  birdseyeConfig: BirdseyeConfig;
  liveMode: LivePlayerMode;
  pip?: boolean;
  containerRef: React.MutableRefObject<HTMLDivElement | null>;
  playerRef?: React.MutableRefObject<HTMLDivElement | null>;
  seamless?: boolean;
  videoFit?: LiveVideoFit;
  onClick?: () => void;
};

export default function BirdseyeLivePlayer({
  className,
  birdseyeConfig,
  liveMode,
  pip,
  containerRef,
  playerRef,
  seamless = false,
  videoFit = "contain",
  onClick,
}: LivePlayerProps) {
  let player;
  if (liveMode == "webrtc") {
    player = (
      <WebRtcPlayer
        className={cn("size-full", VIDEO_FIT_CLASS[videoFit])}
        camera="birdseye"
        pip={pip}
      />
    );
  } else if (liveMode == "mse") {
    if ("MediaSource" in window || "ManagedMediaSource" in window) {
      player = (
        <MSEPlayer
          className={cn("size-full", VIDEO_FIT_CLASS[videoFit])}
          camera="birdseye"
          pip={pip}
        />
      );
    } else {
      player = (
        <div className="w-5xl text-center text-sm">
          iOS 17.1 or greater is required for this live stream type.
        </div>
      );
    }
  } else if (liveMode == "jsmpeg") {
    player = (
      <JSMpegPlayer
        className="flex size-full justify-center overflow-hidden"
        camera="birdseye"
        width={birdseyeConfig.width}
        height={birdseyeConfig.height}
        containerRef={containerRef}
        playbackEnabled={true}
        useWebGL={true}
        rounded={!seamless}
        fit={videoFit}
      />
    );
  } else {
    player = <ActivityIndicator className="w-full [.bg-black_&]:text-white" />;
  }

  return (
    <div
      ref={containerRef}
      className={cn(
        "relative flex w-full cursor-pointer justify-center overflow-hidden",
        !seamless && "rounded-lg md:rounded-2xl",
        className,
      )}
      onClick={onClick}
    >
      <div
        className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center [container-type:size]"
        style={
          {
            "--pic-ar":
              (birdseyeConfig.width || 1) / (birdseyeConfig.height || 1),
          } as React.CSSProperties
        }
      >
        <div
          className={cn(
            "relative",
            videoFit === "contain"
              ? "aspect-[var(--pic-ar)] h-auto w-[min(100%,calc(100cqh*var(--pic-ar)))]"
              : "size-full",
          )}
        >
          <ImageShadowOverlay />
        </div>
      </div>
      <div className="size-full" ref={playerRef}>
        {player}
      </div>
    </div>
  );
}
