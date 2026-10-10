import type { SectionConfigOverrides } from "./types";

const onvif: SectionConfigOverrides = {
  base: {
    sectionDocs: "/configuration/cameras#setting-up-camera-ptz-controls",
    fieldDocs: {
      autotracking: "/configuration/autotracking",
      "autotracking.calibrate_on_startup":
        "/configuration/autotracking#calibration",
      time_sync: "/configuration/cameras#synchronizing-camera-time",
      relative_move: "/configuration/cameras#relative-move-mode",
    },
    fieldOrder: [
      "host",
      "port",
      "user",
      "password",
      "profile",
      "tls_insecure",
      "ignore_time_mismatch",
      "time_sync",
      "relative_move",
      "autotracking",
    ],
    hiddenFields: [
      "autotracking.enabled_in_config",
      "autotracking.movement_weights",
    ],
    advancedFields: [
      "tls_insecure",
      "ignore_time_mismatch",
      "relative_move",
      "autotracking.stream_latency",
    ],
    overrideFields: [],
    restartRequired: ["autotracking.calibrate_on_startup"],
    fieldMessages: [
      {
        key: "autotracking-no-zones",
        health: (ctx) =>
          ctx.fullCameraConfig?.onvif?.autotracking?.enabled === true,
        field: "autotracking.required_zones",
        messageKey: "configMessages.onvif.autotrackingNoZones",
        severity: "error",
        position: "before",
        condition: (ctx) => {
          if (ctx.level !== "camera") return false;
          const zones = ctx.fullCameraConfig?.zones;
          return (
            !zones ||
            typeof zones !== "object" ||
            Object.keys(zones).length === 0
          );
        },
      },
    ],
    uiSchema: {
      host: {
        "ui:options": { size: "sm" },
      },
      password: {
        "ui:widget": "password",
      },
      profile: {
        "ui:widget": "onvifProfile",
      },
      time_sync: {
        ntp_server: {
          "ui:options": { size: "sm" },
        },
        timezone: {
          "ui:options": { size: "sm" },
        },
      },
      relative_move: {
        mode: {
          "ui:options": {
            size: "xs",
            enumI18nPrefix: "onvif.relative_move.mode",
          },
        },
        pan_scale: {
          "ui:options": { size: "xs" },
        },
        tilt_scale: {
          "ui:options": { size: "xs" },
        },
      },
      autotracking: {
        required_zones: {
          "ui:widget": "zoneNames",
        },
        return_preset: {
          "ui:options": { size: "sm" },
          "ui:widget": "ptzPresets",
        },
        stream_latency: {
          "ui:options": { size: "xs" },
        },
        track: {
          "ui:widget": "objectLabels",
        },
        zooming: {
          "ui:options": {
            size: "xs",
            enumI18nPrefix: "onvif.autotracking.zooming",
          },
        },
      },
    },
  },
  // the global section only holds time sync, which cameras inherit
  global: {
    sectionDocs: "/configuration/cameras#synchronizing-camera-time",
    fieldOrder: ["time_sync"],
    hiddenFields: [],
    advancedFields: [],
    restartRequired: [],
    fieldMessages: [],
  },
};

export default onvif;
