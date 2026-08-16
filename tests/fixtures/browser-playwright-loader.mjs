const fakePlaywright = new URL('./fake-playwright.mjs', import.meta.url).href;

export async function resolve(specifier, context, nextResolve) {
  if (specifier === 'playwright') {
    return { shortCircuit: true, url: fakePlaywright };
  }
  return nextResolve(specifier, context);
}
