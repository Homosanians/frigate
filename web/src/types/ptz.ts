type PtzFeature =
  | "pt"
  | "zoom"
  | "pt-r"
  | "zoom-r"
  | "zoom-a"
  | "pt-r-fov"
  | "pt-r-generic"
  | "focus"
  | "home"
  | "home-set";

export type OnvifProfile = {
  name: string;
  token: string;
};

export type PtzPreset = {
  token: string;
  name: string;
};

export type CameraPtzInfo = {
  name: string;
  features: PtzFeature[];
  presets: string[];
  preset_details?: PtzPreset[];
  max_presets?: number | null;
  profiles: OnvifProfile[];
};

export type PtzSource =
  "command" | "api" | "autotrack" | "calibration" | "debug" | "frigate";

export type PtzEntryKind =
  | "connection"
  | "request"
  | "refused"
  | "status"
  | "external_move"
  | "autotrack"
  | "calibration";

export type PtzPosition = {
  pan: number | null;
  tilt: number | null;
  zoom: number | null;
};

export type PtzDebugEntry = {
  id: number;
  seq: number;
  time: number;
  source: PtzSource;
  kind: PtzEntryKind;
  repeats: number;
  until: number | null;
  data: Record<string, unknown>;
};

export type PtzStatus = {
  pan_tilt: string | null;
  zoom: string | null;
  position: PtzPosition | null;
  time: number;
  error: string | null;
};

// a range end is null when the camera reports it as unbounded
export type PtzRelativeSpace = {
  space: string;
  x: [number | null, number | null];
  y: [number | null, number | null];
};

export type PtzCapabilities = {
  features: string[];
  relative_spaces: PtzRelativeSpace[];
  default_relative_space: string | null;
  relative_mode: "fov" | "generic";
};

export type PtzDebugResponse = {
  session: string;
  connected: boolean;
  capabilities: PtzCapabilities | null;
  status: PtzStatus | null;
  seq: number;
  missed: boolean;
  entries: PtzDebugEntry[];
};

// rows are keyed by session as well, since ids start over when Frigate restarts
export type PtzDebugEntryRow = PtzDebugEntry & { key: string };

export type PtzDebugMarker = {
  kind: "marker";
  key: string;
  time: number;
  reason: "missed" | "restart";
};

export type PtzDebugRowItem = PtzDebugEntryRow | PtzDebugMarker;

/** What a camera's ONVIF device service reports; null means it did not answer. */
export type OnvifDeviceInfo = {
  manufacturer: string | null;
  model: string | null;
  firmware_version: string | null;
  /** ONVIF conformance profiles (S, G, T, M, ...), not media profiles. */
  conformance_profiles: string[] | null;
  date_time: {
    type: string | null;
    timezone: string | null;
    daylight_savings: boolean | null;
    utc_time: string | null;
    /** Camera clock minus Frigate's clock. */
    offset_seconds: number | null;
  } | null;
  ntp: {
    from_dhcp: boolean | null;
    servers: string[];
  } | null;
  /** The camera's time sync config and the outcome of the last attempt. */
  time_sync: {
    enabled: boolean;
    ntp_server: string | null;
    timezone: string | null;
    posix_timezone: string | null;
    last_result: {
      time: number;
      success: boolean;
      message: string | null;
    } | null;
  };
};
