/**
 * Live dashboard tile display preferences.
 *
 * Seamless tiles drop the gaps and rounded corners so cameras sit edge to
 * edge like a video wall, on both the default grid and camera group grids.
 *
 * Video fit decides how a picture fills a tile of a different shape, such as
 * a 4:3 substream still in a 16:9 tile.
 */

import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import { LivePage } from "../pages/live.page";
import { cameraBoxes, seedLayout } from "../helpers/grid-layout";

async function openWithPreference(
  frigateApp: FrigateApp,
  path: string,
  key: string,
  value: unknown,
) {
  await frigateApp.goto(path);
  const live = new LivePage(frigateApp.page, !frigateApp.isMobile);
  await expect(live.cameraCard("front_door").first()).toBeVisible({
    timeout: 10_000,
  });

  await seedLayout(frigateApp.page, `${key}:admin`, value);
  await frigateApp.page.reload();
  await expect(live.cameraCard("backyard").first()).toBeVisible({
    timeout: 10_000,
  });
}

// 4x3 grey PNG, so the still is 4:3 while its tile is 16:9
const STILL_4_3 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAADklEQVR4nGNoQAIMODkAXzYSAY5zHKsAAAAASUVORK5CYII=",
  "base64",
);

// garage is restreamed so its tile mounts the MSE <video>, which stays idle
// without motion and leaves the still on screen
async function installFourByThreeGarage(frigateApp: FrigateApp) {
  await frigateApp.installDefaults({
    config: {
      go2rtc: { streams: { garage: ["rtsp://127.0.0.1:8554/garage"] } },
      cameras: { garage: { detect: { width: 640, height: 480 } } },
    },
  });
  await frigateApp.page.route("**/api/*/latest.webp*", (route) =>
    route.fulfill({ contentType: "image/png", body: STILL_4_3 }),
  );
  await frigateApp.page.route("**/api/go2rtc/streams/**", (route) =>
    route.fulfill({
      json: { producers: [{ medias: ["video, recvonly, H264"] }] },
    }),
  );
}

function pictureFit(frigateApp: FrigateApp, camera: string) {
  return frigateApp.page.evaluate((cam) => {
    const card = document.querySelector(`[data-camera='${cam}']`);
    const img = card?.querySelector("img");
    const video = card?.querySelector("video");
    if (!card || !img || !img.complete || !img.naturalWidth) {
      return null;
    }
    const c = card.getBoundingClientRect();
    const i = img.getBoundingClientRect();
    return {
      widthGap: Math.round(c.width - i.width),
      heightGap: Math.round(c.height - i.height),
      imgFit: getComputedStyle(img).objectFit,
      videoFit: video ? getComputedStyle(video).objectFit : null,
    };
  }, camera);
}

function cornerRadii(frigateApp: FrigateApp, camera: string) {
  return frigateApp.page.evaluate((cam) => {
    const card = document.querySelector(`[data-camera='${cam}']`);
    const img = card?.querySelector("img");
    return {
      card: card ? getComputedStyle(card).borderTopLeftRadius : null,
      img: img ? getComputedStyle(img).borderTopLeftRadius : null,
    };
  }, camera);
}

test.describe("Live tile display @high", () => {
  test("default grid keeps gaps and rounded corners", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/");
    const live = new LivePage(frigateApp.page, !frigateApp.isMobile);
    await expect(live.cameraCard("backyard").first()).toBeVisible({
      timeout: 10_000,
    });

    const { front_door: first, backyard: second } = await cameraBoxes(
      frigateApp.page,
      ["front_door", "backyard"] as const,
      "card",
    );
    const gap = frigateApp.isMobile
      ? second.y - (first.y + first.h)
      : second.x - (first.x + first.w);

    expect(gap).toBeGreaterThan(1);
    expect((await cornerRadii(frigateApp, "front_door")).card).not.toBe("0px");
  });

  test("seamless default grid puts tiles edge to edge @mobile", async ({
    frigateApp,
  }) => {
    await openWithPreference(frigateApp, "/", "seamlessLiveTiles", true);

    const { front_door: first, backyard: second } = await cameraBoxes(
      frigateApp.page,
      ["front_door", "backyard"] as const,
      "card",
    );

    if (frigateApp.isMobile) {
      expect(second.y - (first.y + first.h)).toBeCloseTo(0, 0);
    } else {
      expect(second.x - (first.x + first.w)).toBeCloseTo(0, 0);
      expect(second.y).toBeCloseTo(first.y, 0);
    }

    await expect
      .poll(() => cornerRadii(frigateApp, "front_door"))
      .toEqual({ card: "0px", img: "0px" });
  });

  test("seamless group grid puts tiles edge to edge", async ({
    frigateApp,
  }) => {
    test.skip(frigateApp.isMobile, "Draggable grid is desktop-only");

    await openWithPreference(
      frigateApp,
      "/?group=outdoor",
      "seamlessLiveTiles",
      true,
    );

    const { front_door: first, backyard: second } = await cameraBoxes(
      frigateApp.page,
      ["front_door", "backyard"] as const,
      "card",
    );

    expect(second.x - (first.x + first.w)).toBeCloseTo(0, 0);
    expect(second.y).toBeCloseTo(first.y, 0);
    expect((await cornerRadii(frigateApp, "front_door")).card).toBe("0px");
  });

  test("a mismatched still letterboxes by default", async ({ frigateApp }) => {
    await installFourByThreeGarage(frigateApp);
    await frigateApp.goto("/");

    await expect
      .poll(() => pictureFit(frigateApp, "garage"), { timeout: 10_000 })
      .toMatchObject({ heightGap: 0, imgFit: "contain" });
    expect((await pictureFit(frigateApp, "garage"))!.widthGap).toBeGreaterThan(
      10,
    );
  });

  for (const fit of ["fill", "cover"] as const) {
    test(`video fit ${fit} fills the tile with the picture @mobile`, async ({
      frigateApp,
    }) => {
      await installFourByThreeGarage(frigateApp);
      await openWithPreference(frigateApp, "/", "liveVideoFit", fit);

      await expect
        .poll(() => pictureFit(frigateApp, "garage"), { timeout: 10_000 })
        .toEqual({ widthGap: 0, heightGap: 0, imgFit: fit, videoFit: fit });
    });
  }
});
