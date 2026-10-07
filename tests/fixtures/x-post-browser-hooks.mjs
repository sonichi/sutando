export async function resolve(spec, ctx, next) {
  if (spec === 'playwright') return { url: process.env.XSTUB_URL, shortCircuit: true };
  return next(spec, ctx);
}
