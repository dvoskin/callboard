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
 *   1. script.google.com  ->  New project  ->  paste this file
 *   2. Fill in the three settings below
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
// Narrow this if other mail matches. Check it in the Gmail search box first.
const QUERY      = 'has:attachment filename:csv subject:(interaction report) newer_than:2d';

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
      msg.getAttachments().forEach(function (att) {
        if (!/\.csv$/i.test(att.getName())) return;
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

/** Run this by hand first: shows what the query matches without sending. */
function testOnce() {
  const threads = GmailApp.search(QUERY, 0, 10);
  console.log('query matches ' + threads.length + ' thread(s)');
  threads.forEach(function (t) {
    t.getMessages().forEach(function (m) {
      const names = m.getAttachments().map(function (a) {
        return a.getName() + ' (' + a.getSize() + ' bytes)';
      });
      console.log(m.getDate() + ' | ' + m.getSubject() + ' | ' +
                  (names.join(', ') || 'no attachments'));
    });
  });
  if (!threads.length) {
    console.log('No match. Paste QUERY into the Gmail search box and adjust it ' +
                'until the report emails come up, then re-run.');
  }
}
