/**
 * MSE recovery from media the browser cannot decode.
 *
 * go2rtc can start a new viewer on a keyframe it only caught the end of,
 * which the browser rejects. A new connection starts clean, so the player
 * has to reconnect instead of leaving the viewer on a spinner.
 */
import type { Page } from "@playwright/test";
import { test, expect } from "../fixtures/frigate-test";

const CONFIG = {
  go2rtc: {
    streams: { front_door: ["rtsp://127.0.0.1:8554/front_door"] },
    webrtc: { candidates: [], ice_servers: [] },
  },
};

// a box shorter than its own header, which no MP4 parser accepts
const BROKEN_MEDIA = Buffer.from([
  0, 0, 0, 4, 0x6d, 0x6f, 0x6f, 0x76, 0, 0, 0, 0,
]);

// go2rtc answers negotiation, then sends broken media on the connections
// picked by `broken` and nothing on the others
async function mockGo2rtc(page: Page, broken: (connection: number) => boolean) {
  const opened: string[] = [];
  const jsmpeg: string[] = [];

  await page.routeWebSocket("**/live/mse/api/ws**", (ws) => {
    opened.push(new URL(ws.url()).searchParams.get("src") ?? "");
    const connection = opened.length;

    ws.onMessage((message) => {
      const msg = JSON.parse(message.toString());
      if (msg.type !== "mse") {
        return;
      }

      // a codec Chromium can open a source buffer for without H.264 support
      ws.send(
        JSON.stringify({
          type: "mse",
          value: 'video/mp4; codecs="vp09.00.10.08"',
        }),
      );

      if (broken(connection)) {
        ws.send(BROKEN_MEDIA);
      }
    });
  });

  await page.routeWebSocket("**/live/jsmpeg/**", (ws) => {
    jsmpeg.push(ws.url());
  });

  await page.route("**/api/go2rtc/streams/**", (route) =>
    route.fulfill({
      json: {
        producers: [{ medias: ["video, recvonly, VP9"] }],
        consumers: [],
      },
    }),
  );

  return { opened, jsmpeg };
}

test.describe("Live MSE recovery @mobile", () => {
  test("a stream that starts on broken media reconnects", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: CONFIG });
    const { opened } = await mockGo2rtc(
      frigateApp.page,
      (connection) => connection === 1,
    );

    await frigateApp.goto("/#front_door");

    await expect
      .poll(() => opened, { timeout: 10_000 })
      .toEqual(["front_door", "front_door"]);
  });

  test.describe("a stream that never decodes", () => {
    test.use({ expectedErrors: [/MSE error 'startup'/] });

    test("falls back instead of reconnecting forever", async ({
      frigateApp,
    }) => {
      await frigateApp.installDefaults({ config: CONFIG });
      const { opened, jsmpeg } = await mockGo2rtc(frigateApp.page, () => true);

      await frigateApp.goto("/#front_door");

      await expect.poll(() => jsmpeg.length, { timeout: 15_000 }).toBe(1);
      expect(opened.length).toBeGreaterThan(1);
      expect(opened.length).toBeLessThanOrEqual(5);
    });
  });
});
