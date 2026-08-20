/**
 * Gmail -> call-tracker  ·  RingCX Interaction Report forwarder
 *
 * RingCX emails an Interaction Report on a schedule. This takes the CSV off each
 * message and POSTs it to /api/v5/ingest, so /v5 reports from the same report the
 * figures were verified against rather than the live CDR pull.
 *
 * Runs inside YOUR Google account. Nothing else ever reads the mailbox.
 *
 * SETUP
 *   1. script.google.com -> New project -> DELETE the `function myFunction() { }`
 *      placeholder entirely, then paste this file. Pasting inside it nests every
 *      function and none appear in the Run or Trigger dropdowns.
 *   2. Fill in INGEST_KEY below.
 *   3. Run `testOnce` and approve the Gmail + external-request prompts.
 *   4. Triggers (clock icon) -> Add Trigger -> forwardRingCXReports,
 *      Time-driven, Minutes timer, every 15 minutes.
 *
 * Progress is tracked per MESSAGE, not per thread. Gmail groups messages that
 * share a subject into one thread, so an earlier version that excluded already
 * -labelled THREADS went permanently blind to every later report arriving in the
 * same thread. Labels are still applied, but only so a human can see what
 * happened -- they are not the gate.
 */

// ── settings ───────────────────────────────────────────────────
const HOST       = 'https://call-tracker-3z6t.onrender.com';
const INGEST_KEY = 'PASTE_THE_SAME_VALUE_AS_INGEST_API_KEY_ON_RENDER';

const SENDER     = 'ringcx.analytics@ringcentral.com';
const QUERY      = 'from:' + SENDER + ' newer_than:2d';

const LABEL_DONE = 'ringcx/ingested';
const LABEL_FAIL = 'ringcx/failed';
const SEEN_KEY   = 'ringcx_seen_message_ids';
const SEEN_MAX   = 400;

// ── seen-message bookkeeping ───────────────────────────────────
function seen_() {
  const raw = PropertiesService.getScriptProperties().getProperty(SEEN_KEY);
  if (!raw) return {};
  try { return JSON.parse(raw); } catch (e) { return {}; }
}
function markSeen_(map, id) {
  map[id] = Date.now();
  const ids = Object.keys(map);
  if (ids.length > SEEN_MAX) {                       // keep the newest SEEN_MAX
    ids.sort(function (a, b) { return map[a] - map[b]; })
       .slice(0, ids.length - SEEN_MAX)
       .forEach(function (k) { delete map[k]; });
  }
}
function saveSeen_(map) {
  PropertiesService.getScriptProperties().setProperty(SEEN_KEY, JSON.stringify(map));
}

// ── main ───────────────────────────────────────────────────────
function forwardRingCXReports() {
  const done = getOrCreateLabel_(LABEL_DONE);
  const fail = getOrCreateLabel_(LABEL_FAIL);
  const map = seen_();
  const threads = GmailApp.search(QUERY, 0, 25);     // no label filter — see header
  let sent = 0, failed = 0, already = 0, noCsv = 0;

  threads.forEach(function (thread) {
    let ok = false, bad = false;
    thread.getMessages().forEach(function (msg) {
      const id = msg.getId();
      if (map[id]) { already++; return; }            // this MESSAGE is done

      const atts = msg.getAttachments().filter(function (a) {
        return a.getName() && a.getSize() > 0;       // skip inline signature images
      });
      const csvs = atts.filter(function (a) { return /\.csv$/i.test(a.getName()); });
      if (!csvs.length) {
        if (atts.length) {
          console.warn('no CSV on "' + msg.getSubject() + '" — attachments: ' +
                       atts.map(function (a) { return a.getName(); }).join(', ') +
                       '. /api/v5/ingest parses CSV only.');
          noCsv++;
        }
        return;                                      // not marked seen: it may be edited/resent
      }

      let allOk = true;
      csvs.forEach(function (att) {
        const res = post_(att);
        const code = res.getResponseCode();
        if (code === 200) {
          console.log('ingested ' + att.getName() + ' -> ' + res.getContentText());
          sent++;
        } else {
          console.error('ingest FAILED ' + code + ' for ' + att.getName() +
                        ' -> ' + res.getContentText());
          allOk = false; failed++;
        }
      });
      // Only a fully successful message is remembered. A failure stays unseen and
      // is retried next tick, so a report never silently goes missing.
      if (allOk) { markSeen_(map, id); ok = true; } else { bad = true; }
    });
    if (ok) thread.addLabel(done);
    if (bad) thread.addLabel(fail); else if (ok) thread.removeLabel(fail);
  });

  saveSeen_(map);
  console.log('threads ' + threads.length + ' | sent ' + sent + ' | failed ' + failed +
              ' | already done ' + already + ' | messages without a CSV ' + noCsv);
  if (!threads.length) {
    console.warn('Query matched nothing. Check the report really comes from ' + SENDER +
                 ' and arrived within the last 2 days.');
  }
  if (failed && !sent) {
    throw new Error('every ingest failed — check INGEST_KEY matches INGEST_API_KEY on Render');
  }
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

/** Shows what the query matches and what is already recorded. Sends nothing. */
function testOnce() {
  console.log('QUERY: ' + QUERY);
  const map = seen_();
  const threads = GmailApp.search(QUERY, 0, 10);
  console.log('matches ' + threads.length + ' thread(s); ' +
              Object.keys(map).length + ' message(s) already recorded as ingested');
  let csv = 0, other = 0, pending = 0;
  threads.forEach(function (t) {
    console.log('— thread: "' + t.getFirstMessageSubject() + '" (' +
                t.getMessageCount() + ' message(s))');
    t.getMessages().forEach(function (m) {
      const names = m.getAttachments().filter(function (a) {
        return a.getName() && a.getSize() > 0;
      }).map(function (a) {
        if (/\.csv$/i.test(a.getName())) { csv++; } else { other++; }
        return a.getName() + ' (' + Math.round(a.getSize() / 1024) + ' KB)';
      });
      const state = map[m.getId()] ? 'ALREADY INGESTED' : 'PENDING';
      if (!map[m.getId()] && names.length) pending++;
      console.log('   ' + m.getDate() + ' | ' + state + ' | ' +
                  (names.join(', ') || 'NO ATTACHMENTS'));
    });
  });
  console.log('→ ' + csv + ' CSV, ' + other + ' other; ' + pending +
              ' message(s) would be sent on the next run.');
  if (threads.length && !csv) {
    console.log('WARNING: mail matched but no CSV attachment. The report may be ' +
                'arriving as XLSX or as a download link; ingest parses CSV only.');
  }
}

/** Forget everything and re-send every report in the window. Use after a fix. */
function resetAndResend() {
  PropertiesService.getScriptProperties().deleteProperty(SEEN_KEY);
  const done = GmailApp.getUserLabelByName(LABEL_DONE);
  if (done) GmailApp.search(QUERY, 0, 25).forEach(function (t) { t.removeLabel(done); });
  console.log('history cleared — running a full pass now');
  forwardRingCXReports();
}
