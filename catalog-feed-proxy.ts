// Short, authenticated network bridge; XML parsing stays on the scheduled runner.
const allowed = new Set(['prices.carrefour.co.il', 'laibcatalog.co.il', 'shop.hazi-hinam.co.il', 'hazihinamprod01.blob.core.windows.net']);
function allowedUrl(raw: string): URL {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || url.port || !allowed.has(url.hostname)) throw new Error('Source is not allowed');
  return url;
}
Deno.serve(async request => {
  if (request.method !== 'POST') return new Response('POST required', { status: 405 });
  // verify_jwt=true at the Supabase gateway verifies signature/project first.
  // The gateway can exchange API keys for a short-lived JWT, so comparing
  // Authorization byte-for-byte to an environment key is not reliable.
  let role = '';
  try {
    const jwt = (request.headers.get('authorization') || '').replace(/^Bearer /i, '').split('.')[1];
    role = JSON.parse(atob(jwt.replace(/-/g, '+').replace(/_/g, '/'))).role;
  } catch { /* Only verified service-role JWTs may reach upstream sources. */ }
  if (role !== 'service_role') return new Response('Server access required', { status: 403 });
  try {
    let url = allowedUrl((await request.json()).url);
    for (let i = 0; i < 4; i++) {
      const response = await fetch(url, { redirect: 'manual', signal: AbortSignal.timeout(45000), headers: {
        'User-Agent': 'Mozilla/5.0 Kabalator/0.2 official-price-transparency-importer',
        Accept: 'text/html,application/json,application/xml,application/gzip,*/*;q=0.8',
        Referer: `${url.origin}/`,
      }});
      if (response.status >= 300 && response.status < 400 && response.headers.has('location')) {
        url = allowedUrl(new URL(response.headers.get('location')!, url).href);
        await response.body?.cancel(); continue;
      }
      if (!response.ok) { await response.body?.cancel(); return Response.json({error:'Source HTTP error',status:response.status},{status:502}); }
      if (Number(response.headers.get('content-length') || 0) > 32 * 1024 * 1024) { await response.body?.cancel(); return new Response('File exceeds bridge limit', {status:413}); }
      let size = 0;
      const limited = response.body?.pipeThrough(new TransformStream({transform(chunk, controller) {
        size += chunk.byteLength;
        if (size > 32 * 1024 * 1024) { controller.error(new Error('File exceeds bridge limit')); return; }
        controller.enqueue(chunk);
      }}));
      return new Response(limited, {headers:{'Content-Type':response.headers.get('content-type') || 'application/octet-stream','Cache-Control':'no-store'}});
    }
    return new Response('Too many source redirects', {status:502});
  } catch (error) {
    return Response.json({error:error instanceof Error ? error.message : 'Source request failed'}, {status:502});
  }
});
