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
 * GMAIL QUOTA. Every run costs mailbox allowance whether or not there is
 * anything new: a search, a walk of each matching thread, and a fetch of every
 * attachment on a message not seen before. Exhausting it stops the forwarder
 * dead for the rest of the day and the board simply has no report -- which is
 * how 2026-08-27 went. What this file does to stay inside it:
 *
 *   has:attachment      a report without one is nothing this can use
 *   newer_than:1d       the window is re-scanned on EVERY run; 2d doubled the
 *                       standing cost for catch-up nobody was using
 *   includeInlineImages:false
 *                       every signature logo was being fetched and discarded
 *   labels on change    a run where everything was already ingested used to
 *                       rewrite labels on every thread anyway
 *
 * If it still runs out, lower the trigger to every 30 minutes before anything
 * else -- these are daily figures and the board is minutes-stale by design.
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
// in:anywhere because GmailApp.search() skips Trash and archived mail by default,
// and these reports do get filed away -- two of one day's were sitting in Trash,
// never ingested, invisible to the forwarder. A report it cannot see is a gap in
// the board that nothing reports.
// -in:spam is deliberate: `from:` matches a header, which can be spoofed, and spam
// is where a forged lookalike would land. Trash and All Mail are the user's own
// filing; Spam is not.
// has:attachment: a report without one is nothing this script can use, and
// every message the search returns costs quota whether or not it is usable.
// newer_than:1d rather than 2d: the trigger runs every 15 minutes, so a day is
// 96 runs of slack, and the window is scanned on EVERY one of them. Two days
// doubled the standing cost for catch-up nobody was using.
const QUERY      = 'from:' + SENDER + ' in:anywhere -in:spam has:attachment newer_than:1d';

// How many threads to walk per run. The reports thread by subject, so this is
// a handful in practice; the cap only matters when Gmail splits them.
const MAX_THREADS = 12;

// Attachment options. Without these Gmail fetches inline images too -- every
// signature logo on every report -- and the code then throws them away. Paying
// to retrieve something in order to discard it is the cheapest quota to save.
const ATT_OPTS   = { includeInlineImages: false, includeAttachments: true };

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
  try {
    forwardRingCXReports_();
  } catch (e) {
    // Gmail's daily quota reads as a bare exception and stops the run dead.
    // Name it, because "Service invoked too many times" in a stack trace looks
    // like a script bug and is not one -- it is the mailbox's allowance for the
    // day, and it resets on its own.
    const msg = String((e && e.message) || e);
    if (msg.indexOf('too many times') >= 0 || msg.indexOf('Limit Exceeded') >= 0) {
      console.error('Gmail daily quota is used up: ' + msg +
                    '  Nothing was lost -- unposted reports stay unmarked and go ' +
                    'on the next run once the quota resets. If this repeats, ' +
                    'lower the trigger frequency.');
      return;
    }
    throw e;
  }
}

function forwardRingCXReports_() {
  const done = getOrCreateLabel_(LABEL_DONE);
  const fail = getOrCreateLabel_(LABEL_FAIL);
  const map = seen_();
  const threads = GmailApp.search(QUERY, 0, MAX_THREADS);  // no label filter — see header
  let sent = 0, failed = 0, already = 0, noCsv = 0;

  // ONLY THE NEWEST REPORT PER SUBJECT.
  //
  // Each RingCX report is a ROLLING export of the whole day, not an increment:
  // the 3pm one contains everything the 2:45pm one did. The server already
  // relies on that -- a day is only replaced by a report reaching further into
  // it -- so posting all of them was always redundant work, and it was the
  // expensive kind: one attachment fetch each, every fifteen minutes, all day.
  //
  // The superseded ones are marked seen WITHOUT being fetched. That is the
  // saving: the quota goes on the report that will actually be used.
  //
  // Per SUBJECT, because the subjects are how the sales and the
  // Inbound & Scheduling reports are told apart. Keeping only the newest
  // overall would drop one of them entirely.
  const newest = {};                                 // subject -> newest unseen message
  const superseded = [];
  threads.forEach(function (thread) {
    thread.getMessages().forEach(function (msg) {
      if (map[msg.getId()]) { already++; return; }
      const subj = msg.getSubject() || '';
      const prev = newest[subj];
      if (!prev) { newest[subj] = msg; return; }
      if (msg.getDate() > prev.getDate()) { superseded.push(prev); newest[subj] = msg; }
      else { superseded.push(msg); }
    });
  });
  superseded.forEach(function (m) { markSeen_(map, m.getId()); });

  threads.forEach(function (thread) {
    let ok = false, bad = false;
    thread.getMessages().forEach(function (msg) {
      const id = msg.getId();
      if (map[id]) { already++; return; }            // this MESSAGE is done

      const atts = msg.getAttachments(ATT_OPTS).filter(function (a) {
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
        const res = post_(att, msg.getSubject());
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
    // Label writes cost quota too. Only when this run actually did something
    // to the thread -- a run where every message was already ingested used to
    // still rewrite labels on all of them, every fifteen minutes, forever.
    if (ok) thread.addLabel(done);
    if (bad) thread.addLabel(fail);
  });

  saveSeen_(map);
  console.log('threads ' + threads.length + ' | sent ' + sent + ' | failed ' + failed +
              ' | already done ' + already + ' | superseded (not fetched) ' +
              superseded.length + ' | messages without a CSV ' + noCsv);
  if (!threads.length) {
    console.warn('Query matched nothing. Check the report really comes from ' + SENDER +
                 ' and arrived within the last 2 days.');
  }
  if (failed && !sent) {
    throw new Error('every ingest failed — check INGEST_KEY matches INGEST_API_KEY on Render');
  }
}

function post_(attachment, subject) {
  // RingCX mails more than one report from the same address -- the sales
  // Interaction Report and one scoped to Inbound & Scheduling. They cover
  // different agents. Without the subject the server files both under the same
  // day and the second one REPLACES the first, quietly emptying whichever board
  // lost the race. Sending the subject lets each land in its own slot.
  return UrlFetchApp.fetch(HOST + '/api/v5/ingest', {
    method: 'post',
    headers: {
      'X-API-Key': INGEST_KEY,
      'X-Report-Scope': String(subject || ''),
    },
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
      const names = m.getAttachments(ATT_OPTS).filter(function (a) {
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
