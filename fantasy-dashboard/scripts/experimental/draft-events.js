const WebSocket = require('ws');

const url = process.env.ESPN_DRAFT_WS_URL;
if (!url) {
  console.error('Missing ESPN_DRAFT_WS_URL');
  process.exit(1);
}

function printableAscii(buf) {
  return Array.from(buf)
    .map(b => (b >= 32 && b <= 126) ? String.fromCharCode(b) : ' ')
    .join('')
    .replace(/\s+/g, ' ')
    .trim();
}

function extractEvents(text) {
  const patterns = [
    /SELECTED\s+\d+\s+\d+\s+\d+/g,
    /SELECTING\s+\d+\s+\d+/g,
    /CLOCK\s+\d+\s+\d+\s+\d+/g,
    /AUTOSUGGEST\s+\d+/g,
    /AUTODRAFT\s+\d+\s+(?:true|false)/g,
    /LEFT\s+\d+\s+[A-Z0-9-]+/g,
    /TOKEN\s+[A-Z0-9-]+/g
  ];

  const found = [];
  for (const re of patterns) {
    const matches = text.match(re);
    if (matches) found.push(...matches);
  }
  return found;
}

const ws = new WebSocket(url, {
  headers: {
    Origin: 'https://fantasy.espn.com',
    'User-Agent': 'Mozilla/5.0'
  }
});

ws.on('open', () => {
  console.log('Connected');
});

ws.on('message', (data) => {
  const buf = Buffer.isBuffer(data) ? data : Buffer.from(data);
  const text = printableAscii(buf);
  const events = extractEvents(text);

  if (events.length) {
    for (const e of events) console.log(e);
  } else {
    console.log('RAW', text.slice(0, 300));
  }
});

ws.on('close', (code, reason) => {
  console.log('Closed:', code, reason.toString());
});

ws.on('error', (err) => {
  console.error('Error:', err.message);
});
