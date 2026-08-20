/**
 * Gmail -> call-tracker  ·  RingCX Interaction Report forwarder
 *
 * RingCX emails an Interaction Report on a schedule. This picks the attachment
 * off that email and POSTs it to /api/v5/ingest, so /v5 reports from the same
 * report the figures were verified against instead of the live CDR pull.
 *
 * Runs inside YOUR Google account. Nothing else ever reads the mailbox.
 *
 * SETUP (about two minutes)
 *   1. script.google.com  ->  New project  ->  DELETE the placeholder
 *      `function myFunction() { }` entirely, then paste this file. Pasting
 *      inside the placeholder nests everything and nothing shows up in the
 *      Run or Trigger dropdowns.
 *   2. Fill in INGEST_KEY below
 *   3. Run `testOnce` once and approve the Gmail + external-request prompts
 *   4. Triggers (clock icon) -> Add Trigger -> forwardRingCXReports,
 *      Time-driven, Hour timer, every hour
 *
 * It labels what it has sent, so re-running never double-posts and a backlog
 * after an outage is picked up on the next tick.
 */

// ── settings ───────────────────────────────────────────────────
const HOST       = 'https://call-tracker-3z6t.onrender.com';  // your Render URL
const INGEST_KEY = 'PASTE_THE_SAME_VALUE_AS_INGEST_API_KEY_ON_RENDER';

// Every report email comes from this address, so match on the sender rather than
// a subject line that RingCX can reword without telling anyone.
const SENDER     = 'ringcx.analytics@ringcentral.com';
// 7 days, not 1: the label makes re-runs idempotent, so a wide window costs
// nothing and lets a weekend outage catch up on its own instead of losing
// Friday's reports. Each report files under the day its ROWS cover, not the day
// it was sent, so backfilling old mail cannot overwrite today.
const QUERY      = 'from:' + SENDER + ' newer_than:7d';

const LABEL_DONE = 'ringcx/ingested';
const LABEL_FAIL = 'ringcx/failed';

// ── main ───────────────────────────────────────────────────────
function forwardRingCXReports() {
  const done = getOrCreateLabel_(LABEL_DONE);
  const fail = getOrCreateLabel_(LABEL_FAIL);
  // -label: means an already-ingested thread is skipped, so this is safe to
  // run as often as you like and safe to re-run after a failure.
  const threads = GmailApp.search(QUERY + ' -label:' + LABEL_DONE, 0, 25);
  if (!threads.length) { console.log('nothing new'); return; }

  let sent = 0, failed = 0;
  threads.forEach(function (thread) {
    let threadOk = false;
    thread.getMessages().forEach(function (msg) {
      const atts = msg.getAttachments();
      if (!atts.length) return;                     // a notification with no report
      atts.forEach(function (att) {
        if (!/\.csv$/i.test(att.getName())) {
          // Say so out loud. A silently skipped attachment is how a forwarder
          // ends up looking healthy while delivering nothing.
          console.warn('skipped non-CSV attachment: ' + att.getName() +
                       ' — /api/v5/ingest parses CSV only. If RingCX is sending ' +
                       'XLSX, the server needs an XLSX parser.');
          return;
        }
        const res = post_(att);
        const code = res.getResponseCode();
        if (code === 200) {
          console.log('ingested ' + att.getName() + ' -> ' + res.getContentText());
          threadOk = true; sent++;
        } else {
          // Do NOT label as done. The report never silently goes missing:
          // it stays unlabelled and is retried on the next run.
          console.error('ingest failed ' + code + ' for ' + att.getName() +
                        ' -> ' + res.getContentText());
          thread.addLabel(fail); failed++;
        }
      });
    });
    if (threadOk) { thread.addLabel(done); thread.removeLabel(fail); }
  });
  console.log('sent ' + sent + ', failed ' + failed);
  // A forwarder that quietly stops looks exactly like a quiet day downstream.
  // Anything that failed keeps the ringcx/failed label until it succeeds, and
  // /api/v5/ingest/status shows how stale the newest delivered report is.
  if (failed && !sent) throw new Error('every ingest failed — check INGEST_KEY and HOST');
}

function post_(attachment) {
  return UrlFetchApp.fetch(HOST + '/api/v5/ingest', {
    method: 'post',
    headers: { 'X-API-Key': INGEST_KEY },
    payload: { file: attachment.copyBlob().setName(attachment.getName()) },
    muteHttpExceptions: true,
    followRedirects: true,
  });
}

function getOrCreateLabel_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}

/** Run this by hand first: shows what the query matches, and sends nothing. */
function testOnce() {
  console.log('QUERY: ' + QUERY);
  const threads = GmailApp.search(QUERY, 0, 10);
  console.log('matches ' + threads.length + ' thread(s)');
  let csv = 0, other = 0;
  threads.forEach(function (t) {
    t.getMessages().forEach(function (m) {
      const names = m.getAttachments().map(function (a) {
        if (/\.csv$/i.test(a.getName())) { csv++; } else { other++; }
        return a.getName() + ' (' + Math.round(a.getSize() / 1024) + ' KB)';
      });
      console.log(m.getDate() + ' | ' + m.getSubject() + ' | ' +
                  (names.join(', ') || 'NO ATTACHMENTS'));
    });
  });
  console.log('→ ' + csv + ' CSV attachment(s) that would be ingested, ' +
              other + ' other attachment(s) that would be skipped.');
  if (!threads.length) {
    console.log('No match. Check the address is exactly ' + SENDER +
                ' — search "from:' + SENDER + '" in Gmail and see what comes back.');
  } else if (!csv) {
    console.log('WARNING: matched mail but no CSV attachment. The report is ' +
                'probably being sent as XLSX or as a download link, and the ' +
                'ingest endpoint parses CSV only. Say what the log shows above.');
  }
}
