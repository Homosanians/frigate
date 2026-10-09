import { ReactNode, useState } from "react";
import useSWR from "swr";
import axios, { AxiosError } from "axios";
import { useTranslation } from "react-i18next";
import { LuRefreshCw } from "react-icons/lu";
import { toast } from "sonner";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { use24HourTime, useFormattedTimestamp } from "@/hooks/use-date-utils";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { cn } from "@/lib/utils";
import { FrigateConfig } from "@/types/frigateConfig";
import { OnvifDeviceInfo } from "@/types/ptz";

// clocks this far apart break ONVIF authentication on many cameras
const CLOCK_OFFSET_WARNING_SECONDS = 5;

type OnvifDebugInfoProps = {
  cameraName: string;
};

export default function OnvifDebugInfo({ cameraName }: OnvifDebugInfoProps) {
  const { t } = useTranslation(["views/settings"]);
  const { data, error, isValidating, mutate } = useSWR<
    OnvifDeviceInfo,
    AxiosError<{ message?: string }>
  >(`${cameraName}/onvif/info`, {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });

  const unavailable = (
    <span className="text-muted-foreground">
      {t("debug.onvif.unavailable")}
    </span>
  );
  const yesNo = (value: boolean | null | undefined) =>
    value == null
      ? unavailable
      : value
        ? t("debug.onvif.yes")
        : t("debug.onvif.no");

  const formatOffset = (seconds: number) => {
    if (Math.abs(seconds) < 2) {
      return t("debug.onvif.clock.inSync");
    }

    const sign = seconds < 0 ? "-" : "+";
    let rest = Math.abs(seconds);
    const hours = Math.floor(rest / 3600);
    rest -= hours * 3600;
    const minutes = Math.floor(rest / 60);
    rest -= minutes * 60;

    return (
      sign +
      [hours && `${hours}h`, minutes && `${minutes}m`, rest && `${rest}s`]
        .filter(Boolean)
        .join(" ")
    );
  };

  let content: ReactNode;

  if (error) {
    content = (
      <p className="text-danger">
        {t("debug.onvif.error", {
          message: error.response?.data?.message ?? error.message,
        })}
      </p>
    );
  } else if (!data) {
    content = <ActivityIndicator className="mt-4" />;
  } else {
    const dateTime = data.date_time;
    const offset = dateTime?.offset_seconds;

    content = (
      <>
        <InfoSection title={t("debug.onvif.device.title")}>
          <InfoRow label={t("debug.onvif.device.manufacturer")}>
            {data.manufacturer || unavailable}
          </InfoRow>
          <InfoRow label={t("debug.onvif.device.model")}>
            {data.model || unavailable}
          </InfoRow>
          <InfoRow label={t("debug.onvif.device.firmware")}>
            {data.firmware_version || unavailable}
          </InfoRow>
        </InfoSection>

        <InfoSection
          title={t("debug.onvif.profiles.title")}
          desc={t("debug.onvif.profiles.desc")}
        >
          {data.conformance_profiles == null ? (
            unavailable
          ) : data.conformance_profiles.length == 0 ? (
            <span className="text-muted-foreground">
              {t("debug.onvif.profiles.none")}
            </span>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {data.conformance_profiles.map((profile) => (
                <Badge
                  key={profile}
                  variant="secondary"
                  title={t(`debug.onvif.profiles.names.${profile}`, {
                    defaultValue: "",
                  })}
                >
                  {t("debug.onvif.profiles.profile", { profile })}
                </Badge>
              ))}
            </div>
          )}
        </InfoSection>

        <InfoSection title={t("debug.onvif.clock.title")}>
          {dateTime == null ? (
            unavailable
          ) : (
            <>
              <InfoRow label={t("debug.onvif.clock.source")}>
                {dateTime.type || unavailable}
              </InfoRow>
              <InfoRow label={t("debug.onvif.clock.timezone")}>
                {dateTime.timezone ? (
                  <span className="font-mono">{dateTime.timezone}</span>
                ) : (
                  unavailable
                )}
              </InfoRow>
              <InfoRow label={t("debug.onvif.clock.daylightSavings")}>
                {yesNo(dateTime.daylight_savings)}
              </InfoRow>
              <InfoRow label={t("debug.onvif.clock.cameraTime")}>
                {dateTime.utc_time
                  ? dateTime.utc_time.replace("T", " ").replace("Z", " UTC")
                  : unavailable}
              </InfoRow>
              <InfoRow label={t("debug.onvif.clock.offset")}>
                {offset == null ? (
                  unavailable
                ) : (
                  <span
                    className={cn(
                      Math.abs(offset) >= CLOCK_OFFSET_WARNING_SECONDS &&
                        "text-danger",
                    )}
                  >
                    {formatOffset(offset)}
                  </span>
                )}
              </InfoRow>
            </>
          )}
        </InfoSection>

        <InfoSection title={t("debug.onvif.ntp.title")}>
          {data.ntp == null ? (
            unavailable
          ) : (
            <>
              <InfoRow label={t("debug.onvif.ntp.fromDhcp")}>
                {yesNo(data.ntp.from_dhcp)}
              </InfoRow>
              <InfoRow label={t("debug.onvif.ntp.servers")}>
                {data.ntp.servers.length > 0 ? (
                  <span className="break-all">
                    {data.ntp.servers.join(", ")}
                  </span>
                ) : (
                  <span className="text-muted-foreground">
                    {t("debug.onvif.ntp.noServers")}
                  </span>
                )}
              </InfoRow>
            </>
          )}
        </InfoSection>

        <TimeSyncSection
          cameraName={cameraName}
          timeSync={data.time_sync}
          onSynced={() => mutate()}
        />
      </>
    );
  }

  return (
    <div className="mt-2 flex w-full flex-col gap-3 text-sm">
      <div className="flex flex-row items-start justify-between gap-2">
        <p className="text-muted-foreground">{t("debug.onvif.desc")}</p>
        <Button
          size="sm"
          variant="outline"
          aria-label={t("debug.onvif.refresh")}
          title={t("debug.onvif.refresh")}
          disabled={isValidating}
          onClick={() => mutate()}
        >
          <LuRefreshCw
            className={cn("size-4", isValidating && "animate-spin")}
          />
        </Button>
      </div>
      {content}
    </div>
  );
}

type TimeSyncSectionProps = {
  cameraName: string;
  timeSync: OnvifDeviceInfo["time_sync"];
  onSynced: () => void;
};

function TimeSyncSection({
  cameraName,
  timeSync,
  onSynced,
}: TimeSyncSectionProps) {
  const { t } = useTranslation(["views/settings", "common"]);
  const { data: config } = useSWR<FrigateConfig>("config");
  const isAdmin = useIsAdmin();
  const is24Hour = use24HourTime(config);
  const [syncing, setSyncing] = useState(false);

  const lastResult = timeSync.last_result;
  const lastResultTime = useFormattedTimestamp(
    lastResult?.time ?? 0,
    is24Hour
      ? t("time.formattedTimestampMonthDayHourMinute.24hour", { ns: "common" })
      : t("time.formattedTimestampMonthDayHourMinute.12hour", { ns: "common" }),
    config?.ui.timezone,
  );

  const notSet = (
    <span className="text-muted-foreground">
      {t("debug.onvif.timeSync.notSet")}
    </span>
  );

  const syncTime = async () => {
    setSyncing(true);

    try {
      await axios.post(`${cameraName}/onvif/time_sync`);
      toast.success(t("debug.onvif.timeSync.toast.success"), {
        position: "top-center",
      });
    } catch (error) {
      const axiosError = error as AxiosError<{ message?: string }>;
      toast.error(
        t("debug.onvif.timeSync.toast.error", {
          message: axiosError.response?.data?.message ?? axiosError.message,
        }),
        { position: "top-center" },
      );
    } finally {
      setSyncing(false);
      onSynced();
    }
  };

  return (
    <InfoSection
      title={t("debug.onvif.timeSync.title")}
      desc={t("debug.onvif.timeSync.desc")}
    >
      {!timeSync.enabled ? (
        <span className="text-muted-foreground">
          {t("debug.onvif.timeSync.disabled")}
        </span>
      ) : (
        <>
          <InfoRow label={t("debug.onvif.timeSync.ntpServer")}>
            {timeSync.ntp_server ?? notSet}
          </InfoRow>
          <InfoRow label={t("debug.onvif.timeSync.timezone")}>
            {timeSync.timezone ? (
              <>
                {timeSync.timezone}{" "}
                <span className="font-mono text-muted-foreground">
                  ({timeSync.posix_timezone})
                </span>
              </>
            ) : (
              notSet
            )}
          </InfoRow>
          <InfoRow label={t("debug.onvif.timeSync.lastResult")}>
            {lastResult == null ? (
              <span className="text-muted-foreground">
                {t("debug.onvif.timeSync.notApplied")}
              </span>
            ) : lastResult.success ? (
              t("debug.onvif.timeSync.applied", { time: lastResultTime })
            ) : (
              <span className="text-danger">
                {t("debug.onvif.timeSync.failed", {
                  time: lastResultTime,
                  message: lastResult.message,
                })}
              </span>
            )}
          </InfoRow>
          {isAdmin && (
            <Button
              size="sm"
              variant="outline"
              className="mt-1 self-start"
              disabled={syncing || (!timeSync.ntp_server && !timeSync.timezone)}
              onClick={syncTime}
            >
              {syncing && <ActivityIndicator className="mr-2" size={16} />}
              {t("debug.onvif.timeSync.syncNow")}
            </Button>
          )}
        </>
      )}
    </InfoSection>
  );
}

type InfoSectionProps = {
  title: string;
  desc?: string;
  children: ReactNode;
};

function InfoSection({ title, desc, children }: InfoSectionProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <Separator className="mb-1" />
      <div className="font-medium text-primary">{title}</div>
      {desc && <p className="text-xs text-muted-foreground">{desc}</p>}
      {children}
    </div>
  );
}

type InfoRowProps = {
  label: string;
  children: ReactNode;
};

function InfoRow({ label, children }: InfoRowProps) {
  return (
    <div className="flex flex-row justify-between gap-3">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      {/* values like a POSIX timezone have no spaces to wrap at */}
      <span className="min-w-0 break-words text-end">{children}</span>
    </div>
  );
}
