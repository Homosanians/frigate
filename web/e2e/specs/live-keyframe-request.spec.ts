/**
 * Keyframe request when a live stream starts.
 *
 * A stream that starts between keyframes shows nothing until the next
 * one, so a camera with ONVIF is asked to send one. go2rtc only passes a
 * keyframe to a player it has attached, which it has once it answers MSE
 * codec negotiation, so the request must not go out before that answer.
 */
import type { Page } from "@playwright/test";
import { test, expect } from "../fixtures/frigate-test";

const ANSWER_DELAY_MS = 300;

function config(onvifHost: string) {
  return {
    go2rtc: {
      streams: { front_door: ["rtsp://127.0.0.1:8554/front_door"] },
      webrtc: { candidates: [], ice_servers: [] },
    },
    cameras: { front_door: { onvif: { host: onvifHost } } },
  };
}

// go2rtc answers MSE negotiation late and then sends nothing
async function mockLive(page: Page) {
  const requests: string[] = [];
  const beforeAnswer: number[] = [];

  await page.route("**/api/*/onvif/sync_point", (route) => {
    requests.push(new URL(route.request().url()).pathname);
    return route.fulfill({ json: { success: true, requested: true } });
  });

  await page.routeWebSocket("**/live/mse/api/ws**", (ws) => {
    ws.onMessage((message) => {
      const msg = JSON.parse(message.toString());
      if (msg.type !== "mse") {
        return;
      }

      setTimeout(() => {
        beforeAnswer.push(requests.length);
        ws.send(
          JSON.stringify({
            type: "mse",
            value: 'video/mp4; codecs="avc1.640029"',
          }),
        );
      }, ANSWER_DELAY_MS);
    });
  });

  await page.route("**/api/go2rtc/streams/**", (route) =>
    route.fulfill({
      json: {
        producers: [{ medias: ["video, recvonly, H264"] }],
        consumers: [],
      },
    }),
  );

  await page.route("**/api/front_door/ptz/info**", (route) =>
    route.fulfill({
      json: {
        name: "front_door",
        features: [],
        presets: [],
        preset_details: [],
        max_presets: null,
        profiles: [],
      },
    }),
  );

  // the player adds its source buffer while it handles go2rtc's answer
  await page.addInitScript(() => {
    const addSourceBuffer = MediaSource.prototype.addSourceBuffer;
    MediaSource.prototype.addSourceBuffer = function (type: string) {
      (window as unknown as { answered: boolean }).answered = true;
      return addSourceBuffer.call(this, type);
    };
  });

  return { requests, beforeAnswer };
}

test.describe("Live keyframe request @mobile", () => {
  test("a camera with ONVIF is asked once go2rtc has attached the player", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: config("10.0.0.5") });
    const { requests, beforeAnswer } = await mockLive(frigateApp.page);

    await frigateApp.goto("/#front_door");

    await expect
      .poll(() => requests, { timeout: 10_000 })
      .toEqual(["/api/front_door/onvif/sync_point"]);
    expect(beforeAnswer).toEqual([0]);
  });

  test("a camera without ONVIF is not asked", async ({ frigateApp }) => {
    await frigateApp.installDefaults({ config: config("") });
    const { requests } = await mockLive(frigateApp.page);

    await frigateApp.goto("/#front_door");
    await frigateApp.page.waitForFunction(
      () => (window as unknown as { answered?: boolean }).answered === true,
      undefined,
      { timeout: 10_000 },
    );

    // requests reach the mocks in the order the page sends them, so one sent
    // while the answer was handled would be recorded before this one returns
    await frigateApp.page.evaluate(() => fetch("api/config"));
    expect(requests).toEqual([]);
  });
});
