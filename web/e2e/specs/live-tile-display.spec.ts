/**
 * Live dashboard tile display preferences.
 *
 * Seamless tiles drop the gaps and rounded corners so cameras sit edge to
 * edge like a video wall, on both the default grid and camera group grids.
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
});
