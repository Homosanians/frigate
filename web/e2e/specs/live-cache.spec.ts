/**
 * Live stream cache.
 *
 * With the cache on, the MSE player connects to Frigate's relay, which sends
 * the frames since the last keyframe at once and then live frames. The
 * player loads that backlog and starts at its end. The relay is mocked with
 * a small VP9 clip, one fragment per frame, whose timestamps are moved far
 * from zero as they are on a stream that has been up for a while.
 */
import type { Page } from "@playwright/test";
import { test, expect } from "../fixtures/frigate-test";
import { LIVE_CACHE_VP9 as MEDIA } from "../fixtures/mock-data/live-cache-media";
const REPLY = 'video/mp4; codecs="vp09.00.10.08"';
// four seconds at 10 fps
const BACKLOG_FRAMES = 40;
const TICK_OFFSET = 36_000_000;

const STREAMS = {
  front_door: ["rtsp://127.0.0.1:8554/front_door"],
  backyard: ["rtsp://127.0.0.1:8554/backyard"],
};

function config(cached: boolean) {
  return {
    go2rtc: {
      streams: STREAMS,
      webrtc: { candidates: [], ice_servers: [] },
    },
    cameras: {
      front_door: { live: { cache: { enabled: cached } } },
      backyard: { live: { cache: { enabled: cached } } },
    },
  };
}

function shiftDecodeTime(fragment: Buffer): Buffer {
  const copy = Buffer.from(fragment);
  const tfdt = copy.indexOf("tfdt", 0, "latin1");

  if (copy[tfdt + 4] === 1) {
    copy.writeBigUInt64BE(
      copy.readBigUInt64BE(tfdt + 8) + BigInt(TICK_OFFSET),
      tfdt + 8,
    );
  } else {
    copy.writeUInt32BE(copy.readUInt32BE(tfdt + 8) + TICK_OFFSET, tfdt + 8);
  }

  return copy;
}

// the clip as go2rtc would send it: an init segment, then one fragment a frame
function readMedia() {
  const boxes: { type: string; data: Buffer }[] = [];

  for (let offset = 0; offset + 8 <= MEDIA.length;) {
    const size = MEDIA.readUInt32BE(offset);
    boxes.push({
      type: MEDIA.toString("latin1", offset + 4, offset + 8),
      data: MEDIA.subarray(offset, offset + size),
    });
    offset += size;
  }

  const first = boxes.findIndex((box) => box.type === "moof");
  const init = Buffer.concat(boxes.slice(0, first).map((box) => box.data));
  const fragments: Buffer[] = [];

  for (let i = first; i + 1 < boxes.length; i++) {
    if (boxes[i].type === "moof" && boxes[i + 1].type === "mdat") {
      fragments.push(
        shiftDecodeTime(Buffer.concat([boxes[i].data, boxes[i + 1].data])),
      );
    }
  }

  return { init, fragments };
}

type Relay = {
  cache: "works" | "down";
  // bytes of filler added to every cached frame
  padding?: number;
};

// a top-level box players skip, to make a fragment as large as a real one
function pad(fragment: Buffer, bytes: number): Buffer {
  const free = Buffer.alloc(bytes);
  free.writeUInt32BE(bytes, 0);
  free.write("free", 4, "latin1");
  return Buffer.concat([fragment, free]);
}

// records which server each MSE socket went to, in open order
async function mockLive(page: Page, relay: Relay) {
  const opened: string[] = [];
  const media = readMedia();

  await page.routeWebSocket("**/live/cache/ws**", (ws) => {
    opened.push(`cache:${new URL(ws.url()).searchParams.get("src")}`);

    if (relay.cache === "down") {
      ws.close();
      return;
    }

    ws.onMessage((message) => {
      if (JSON.parse(message.toString()).type !== "mse") {
        return;
      }

      ws.send(JSON.stringify({ type: "mse", value: REPLY }));
      ws.send(media.init);
      media.fragments
        .slice(0, BACKLOG_FRAMES)
        .forEach((fragment) =>
          ws.send(relay.padding ? pad(fragment, relay.padding) : fragment),
        );
      ws.send(JSON.stringify({ type: "live" }));

      // the rest arrives as live frames do, one at a time
      let next = BACKLOG_FRAMES;
      const timer = setInterval(() => {
        if (next >= media.fragments.length) {
          clearInterval(timer);
          return;
        }

        ws.send(media.fragments[next++]);
      }, 100);
      ws.onClose(() => clearInterval(timer));
    });
  });

  await page.routeWebSocket("**/live/mse/api/ws**", (ws) => {
    opened.push(`go2rtc:${new URL(ws.url()).searchParams.get("src")}`);

    ws.onMessage((message) => {
      if (JSON.parse(message.toString()).type === "mse") {
        ws.send(JSON.stringify({ type: "mse", value: REPLY }));
      }
    });
  });

  await page.route("**/api/go2rtc/streams/**", (route) =>
    route.fulfill({
      json: {
        producers: [{ medias: ["video, recvonly, VP9"] }],
        consumers: [],
      },
    }),
  );

  // where playback was when the first frame showed
  await page.addInitScript(() => {
    document.addEventListener(
      "loadeddata",
      (event) => {
        const video = event.target as HTMLVideoElement;
        const state = window as unknown as {
          firstFrame?: { time: number; start: number };
        };

        if (
          video.tagName === "VIDEO" &&
          !state.firstFrame &&
          video.buffered.length
        ) {
          state.firstFrame = {
            time: video.currentTime,
            start: video.buffered.start(0),
          };
        }
      },
      true,
    );
  });

  return opened;
}

test.describe("Live stream cache @mobile", () => {
  test("the player starts at the end of the cached frames", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: config(true) });
    const opened = await mockLive(frigateApp.page, { cache: "works" });
    const errors: string[] = [];
    frigateApp.page.on("console", (message) => {
      if (message.type() === "error" && message.text().includes("MSE error")) {
        errors.push(message.text());
      }
    });

    await frigateApp.goto("/#front_door");

    const handle = await frigateApp.page.waitForFunction(
      () =>
        (window as unknown as { firstFrame?: { time: number; start: number } })
          .firstFrame,
      undefined,
      { timeout: 10_000 },
    );
    const firstFrame = (await handle.jsonValue()) as {
      time: number;
      start: number;
    };

    // the clip's timestamps were moved far from zero
    expect(firstFrame.start).toBeGreaterThan(100);
    // four seconds were cached, and playback starts half a second from their end
    expect(firstFrame.time - firstFrame.start).toBeGreaterThan(3);
    expect(firstFrame.time - firstFrame.start).toBeLessThan(4.1);

    // and carries on with the live frames
    await expect
      .poll(
        () =>
          frigateApp.page.evaluate(
            () => document.querySelector("video")?.currentTime ?? 0,
          ),
        { timeout: 5_000 },
      )
      .toBeGreaterThan(firstFrame.time + 0.5);

    expect(opened).toEqual(["cache:front_door"]);
    expect(errors).toEqual([]);
  });

  test("a failing cache socket falls back to go2rtc", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: config(true) });
    const opened = await mockLive(frigateApp.page, { cache: "down" });

    await frigateApp.goto("/#front_door");

    await expect
      .poll(() => opened, { timeout: 10_000 })
      .toEqual(["cache:front_door", "go2rtc:front_door"]);
  });

  test("a camera without the cache plays from go2rtc", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: config(false) });
    const opened = await mockLive(frigateApp.page, { cache: "works" });

    await frigateApp.goto("/#front_door");

    await expect
      .poll(() => opened, { timeout: 10_000 })
      .toEqual(["go2rtc:front_door"]);
  });

  test("an open dashboard keeps its cameras' streams warm", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: config(true) });
    await mockLive(frigateApp.page, { cache: "works" });
    const warm: { sources: string[]; messages: string[]; closed: boolean }[] =
      [];

    await frigateApp.page.routeWebSocket("**/live/cache/warm**", (ws) => {
      const socket = {
        sources: new URL(ws.url()).searchParams.getAll("src"),
        messages: [] as string[],
        closed: false,
      };
      warm.push(socket);
      ws.onMessage((message) => socket.messages.push(message.toString()));
      ws.onClose(() => {
        socket.closed = true;
      });
    });

    await frigateApp.goto("/");

    // garage does not have the cache on
    await expect
      .poll(
        () => warm.filter((socket) => !socket.closed).map((s) => s.sources),
        { timeout: 10_000 },
      )
      .toEqual([["backyard", "front_door"]]);
    const open = warm.find((socket) => !socket.closed)!;
    await expect.poll(() => open.messages.length).toBe(1);
    expect(JSON.parse(open.messages[0]).type).toBe("warm");

    // a hidden tab lets the streams go
    await frigateApp.page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        value: "hidden",
        configurable: true,
      });
      document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
    });

    await expect.poll(() => open.closed).toBe(true);
  });

  test("a backlog larger than the player's staging buffer plays", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: config(true) });
    // 40 frames of 64 KB are more than the 2 MB the player stages appends in
    await mockLive(frigateApp.page, { cache: "works", padding: 65536 });
    const errors: string[] = [];
    frigateApp.page.on("pageerror", (error) => errors.push(error.message));

    await frigateApp.goto("/#front_door");

    const handle = await frigateApp.page.waitForFunction(
      () =>
        (window as unknown as { firstFrame?: { time: number; start: number } })
          .firstFrame,
      undefined,
      { timeout: 10_000 },
    );
    const firstFrame = (await handle.jsonValue()) as {
      time: number;
      start: number;
    };

    expect(firstFrame.time - firstFrame.start).toBeGreaterThan(3);
    await expect
      .poll(
        () =>
          frigateApp.page.evaluate(
            () => document.querySelector("video")?.currentTime ?? 0,
          ),
        { timeout: 5_000 },
      )
      .toBeGreaterThan(firstFrame.time + 0.5);
    expect(errors).toEqual([]);
  });

  test("the cache is tried again a while after it failed", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: config(true) });
    const relay: Relay = { cache: "down" };
    const opened = await mockLive(frigateApp.page, relay);

    await frigateApp.goto("/#front_door");
    await expect
      .poll(() => opened, { timeout: 10_000 })
      .toEqual(["cache:front_door", "go2rtc:front_door"]);

    // the relay is back, and two minutes have passed on the page's clock
    relay.cache = "works";
    await frigateApp.page.evaluate(() => {
      const now = Date.now;
      Date.now = () => now() + 120_000;
    });

    await frigateApp.page.evaluate(() => {
      location.hash = "";
    });
    await expect(
      frigateApp.page.locator("[data-camera='front_door']").first(),
    ).toBeVisible();
    await frigateApp.page.evaluate(() => {
      location.hash = "#front_door";
    });

    await expect
      .poll(() => opened, { timeout: 10_000 })
      .toEqual(["cache:front_door", "go2rtc:front_door", "cache:front_door"]);
  });

  test("a transcoded stream plays from go2rtc", async ({ frigateApp }) => {
    // transcoding only runs while someone watches, so the cache leaves it alone
    await frigateApp.installDefaults({
      config: {
        go2rtc: {
          streams: STREAMS,
          webrtc: { candidates: [], ice_servers: [] },
        },
        cameras: {
          front_door: {
            live: {
              cache: { enabled: true },
              streams: { "720p": "front_door_transcode_720p" },
              transcode: {
                enabled: true,
                source: "front_door",
                qualities: [{ height: 720, bitrate: 1200 }],
              },
            },
          },
        },
      },
    });
    const opened = await mockLive(frigateApp.page, { cache: "works" });

    // the viewer picked the transcoded stream for this camera
    await frigateApp.goto("/");
    await frigateApp.page.evaluate(async () => {
      await new Promise<void>((resolve, reject) => {
        const request = indexedDB.open("keyval-store", 1);
        request.onupgradeneeded = () =>
          request.result.createObjectStore("keyval");
        request.onerror = () => reject(request.error);
        request.onsuccess = () => {
          const tx = request.result.transaction("keyval", "readwrite");
          tx.objectStore("keyval").put(
            "front_door_transcode_720p",
            "front_door-stream:admin",
          );
          tx.oncomplete = () => resolve();
          tx.onerror = () => reject(tx.error);
        };
      });
    });
    await frigateApp.goto("/#front_door");

    await expect
      .poll(() => opened, { timeout: 10_000 })
      .toEqual(["go2rtc:front_door_transcode_720p"]);
  });

  test("a dashboard that opens in a hidden tab holds no streams", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ config: config(true) });
    await mockLive(frigateApp.page, { cache: "works" });
    const warm: number[] = [];
    await frigateApp.page.routeWebSocket("**/live/cache/warm**", () => {
      warm.push(Date.now());
    });
    await frigateApp.page.addInitScript(() => {
      Object.defineProperty(document, "visibilityState", {
        value: "hidden",
        configurable: true,
      });
    });

    await frigateApp.goto("/");
    await expect(
      frigateApp.page.locator("[data-camera='front_door']").first(),
    ).toBeVisible();
    // requests reach the mocks in the order the page sends them, so a socket
    // opened while hidden would be recorded before this returns
    await frigateApp.page.evaluate(() => fetch("api/config"));

    const shownAt = Date.now();
    await frigateApp.page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        value: "visible",
        configurable: true,
      });
      document.dispatchEvent(new Event("visibilitychange", { bubbles: true }));
    });

    await expect.poll(() => warm.length, { timeout: 10_000 }).toBe(1);
    expect(warm[0]).toBeGreaterThanOrEqual(shownAt);
  });
});
