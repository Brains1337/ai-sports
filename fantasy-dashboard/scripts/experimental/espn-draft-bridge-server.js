const http = require('http');

const PORT = process.env.PORT || 8787;
const TOKEN = process.env.DRAFT_BRIDGE_TOKEN || 'REPLACE_ME';

const server = http.createServer((req, res) => {
  if (req.method !== 'POST' || req.url !== '/espn/draft-event') {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ ok: false, error: 'not_found' }));
  }

  const auth = req.headers.authorization || '';
  if (TOKEN && auth !== `Bearer ${TOKEN}`) {
    res.writeHead(401, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ ok: false, error: 'unauthorized' }));
  }

  let body = '';
  req.on('data', chunk => {
    body += chunk;
    if (body.length > 2_000_000) req.destroy();
  });

  req.on('end', () => {
    try {
      const evt = JSON.parse(body || '{}');
      console.log(JSON.stringify({ receivedAt: new Date().toISOString(), evt }));
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    } catch (e) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'bad_json' }));
    }
  });
});

server.listen(PORT, () => {
  console.log(`Draft bridge listening on :${PORT}`);
});
