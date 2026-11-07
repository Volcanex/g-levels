// Exercise the shipped flat-mark fallback on a software renderer, keeping all
// page content, the backdrop and the real cursor motor painted and running.
export default async (page, mark) => {
  await mark('load');
  await page.context().addInitScript(() => {
    const get = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(type, ...args) {
      return /webgl/i.test(type) ? null : get.call(this, type, ...args);
    };
  });
  await page.reload({ waitUntil: 'networkidle' });
  await page.locator('.lp-attractor[data-on]').waitFor();
  await page.waitForTimeout(5000);
  await mark('settled');
};
