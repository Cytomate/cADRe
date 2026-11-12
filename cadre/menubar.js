// cADRe menu bar (macOS). JavaScript for Automation: runs with the osascript that ships with
// macOS, so it needs no Python packages and no compiler.
//
//   /usr/bin/osascript -l JavaScript menubar.js [port]     (python3 -m cadre menubar starts it)
//
// It only reads the dashboard's /api/snapshot; it never touches agents. "Pause hooks" creates
// ~/.cadre/paused (CADRE_HOME respected), which emit.py checks before recording anything.
// When a detection rule flags an agent (the snapshot's "alerts"), it shows a notification.
ObjC.import('Cocoa');

var argv = $.NSProcessInfo.processInfo.arguments.js.map(function (a) { return a.js; });
var PORT = /^\d+$/.test(argv[argv.length - 1]) ? argv[argv.length - 1] : '8765';
var BASE = 'http://127.0.0.1:' + PORT;
var EVERY = 3;                                    // seconds between polls
var MAX_ROWS = 12;
var env = $.NSProcessInfo.processInfo.environment;
var home = env.objectForKey('CADRE_HOME');
var CADRE_HOME = home.isNil() ? $.NSHomeDirectory().js + '/.cadre' : home.js;
var PAUSED = CADRE_HOME + '/paused';
var LABEL = 'com.cadre.dashboard';
var DEBUG = !env.objectForKey('CADRE_MENUBAR_DEBUG').isNil();
var GLYPH ={working: '●', waiting: '◆', idle: '○', suspended: '‖', sleeping: '·'};

var app = $.NSApplication.sharedApplication;
app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);   // no Dock icon
var item = $.NSStatusBar.systemStatusBar.statusItemWithLength($.NSVariableStatusItemLength);
var session = $.NSURLSession.sessionWithConfigurationDelegateDelegateQueue(
  $.NSURLSessionConfiguration.ephemeralSessionConfiguration, $(), $.NSOperationQueue.mainQueue);
session.configuration.timeoutIntervalForRequest = 5;

var state = {snap: null, error: null, inflight: false, open: false};
var alertMark = Date.now() / 1000;                // alerts up to here were shown (or predate the icon)

function fm() { return $.NSFileManager.defaultManager; }
function paused() { return fm().fileExistsAtPath(PAUSED); }

function setPaused(on) {
  if (on) {
    fm().createDirectoryAtPathWithIntermediateDirectoriesAttributesError(CADRE_HOME, true, $(), $());
    $('paused by the cADRe menu bar\n').writeToFileAtomicallyEncodingError(PAUSED, true, $.NSUTF8StringEncoding, $());
  } else {
    fm().removeItemAtPathError(PAUSED, $());
  }
}

function run(path, args, wait) {
  var t = $.NSTask.alloc.init;
  t.launchPath = path;
  t.arguments = $(args);
  t.standardOutput = $.NSFileHandle.fileHandleWithNullDevice;
  t.standardError = $.NSFileHandle.fileHandleWithNullDevice;
  try { t.launch; } catch (e) { return false; }
  if (wait) t.waitUntilExit;
  return true;
}

// Unload the LaunchAgent (so KeepAlive doesn't bring it back; Start or the next login does),
// then stop the dashboard that answered the last poll, in case it was started by hand.
function stopDashboard(wait) {
  var pid = state.snap && state.snap.tracker && Number(state.snap.tracker.pid);
  run('/bin/sh', ['-c', 'launchctl bootout gui/$(id -u)/' + LABEL + ' 2>/dev/null' +
      (pid > 1 ? '; kill ' + pid + ' 2>/dev/null' : '') + '; exit 0'], wait);
  state.snap = null;
  state.error = 'stopped';
}

ObjC.registerSubclass({
  name: 'CadreMenuTarget',
  methods: {
    'tick:': {types: ['void', ['id']], implementation: function () { poll(); }},
    'open:': {types: ['void', ['id']], implementation: function () {
      $.NSWorkspace.sharedWorkspace.openURL($.NSURL.URLWithString(BASE + '/'));
    }},
    'pause:': {types: ['void', ['id']], implementation: function () { setPaused(!paused()); render(); }},
    'start:': {types: ['void', ['id']], implementation: function () {
      // the LaunchAgent written by `cadre setup`; kickstart starts it if it isn't running
      // (bootstrap loads it again if `launchctl bootout` unloaded it)
      run('/bin/sh', ['-c', 'd=gui/$(id -u); launchctl kickstart $d/' + LABEL +
          ' || launchctl bootstrap $d "$HOME/Library/LaunchAgents/' + LABEL + '.plist"']);
      state.error = 'starting…';
      render();
    }},
    'stop:': {types: ['void', ['id']], implementation: function () { stopDashboard(false); render(); }},
    // Quit = the whole of cADRe: dashboard and icon (hooks still append to the event log)
    'quit:': {types: ['void', ['id']], implementation: function () { stopDashboard(true); app.terminate($()); }},
    'quiticon:': {types: ['void', ['id']], implementation: function () { app.terminate($()); }},
    // rebuilding a menu while it is open makes it flicker, so it waits until it closes
    'menuWillOpen:': {types: ['void', ['id']], implementation: function () { state.open = true; poll(); }},
    'menuDidClose:': {types: ['void', ['id']], implementation: function () { state.open = false; render(); }},
  },
});
var target = $.CadreMenuTarget.alloc.init;

function add(menu, title, action, enabled) {
  var mi = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent(title, action || $(), '');
  if (action) mi.target = target;
  if (enabled === false || !action) mi.enabled = false;
  menu.addItem(mi);
  return mi;
}

// alerts: begin (plain JavaScript, run by tests/test_menubar.py; the same rules as cadre/tray.py)
function short(s, n) {
  s = String(s || '').replace(/\s+/g, ' ').trim();
  return s.length <= n ? s : s.slice(0, n - 1) + '…';
}

// [alerts of the snapshot newer than `mark`, newest first; the new mark]. The dashboard lists
// its recent alerts in every snapshot (cadre/respond/alerts.py); each is shown once.
function freshAlerts(snap, mark) {
  var fresh = ((snap && snap.alerts) || []).filter(function (a) { return (Number(a.ts) || 0) > mark; });
  return [fresh, fresh.reduce(function (m, a) { return Math.max(m, Number(a.ts)); }, mark)];
}

// [title, text] of one notification for these alerts
function alertText(alerts) {
  var who = {}, title, lines;
  alerts.forEach(function (a) { who[(a.agent || '') + '\n' + (a.project || '')] = 1; });
  if (Object.keys(who).length === 1) {
    var a = alerts[0];
    title = 'cADRe: ' + (a.agent || 'agent') + (a.project ? ' · ' + a.project : '');
    lines = alerts.slice(0, 3).map(function (x) { return short(x.text, 80); });
  } else {
    title = 'cADRe: ' + alerts.length + ' risk flags';
    lines = alerts.slice(0, 3).map(function (x) {
      return short((x.project || x.agent || 'agent') + ': ' + x.text, 80);
    });
  }
  if (alerts.length > 3) lines.push('+' + (alerts.length - 3) + ' more');
  return [title, lines.join('\n')];
}
// alerts: end

// A Notification Centre banner, through `display notification` (macOS lists it under Script
// Editor). Title and text go in as arguments, never into the script: they hold what an agent
// ran, and a quote in a command must not become AppleScript.
function notify(title, text) {
  run('/usr/bin/osascript', ['-e', 'on run argv',
      '-e', 'display notification (item 2 of argv) with title (item 1 of argv)',
      '-e', 'end run', title, text]);
}

// The Formation mark, drawn by scripts/make_icons.py: 36 px shown at 18 pt (sharp on Retina).
// menubar.png and menubar-off.png are black templates, so macOS tints them for a light or dark
// menu bar; menubar-waiting.png keeps its amber. An SF Symbol stands in if a file is missing.
var STATIC = $.NSString.stringWithString(argv.filter(function (a) { return /menubar\.js$/.test(a); })[0] || '')
  .stringByDeletingLastPathComponent.js + '/static/';
var ICONS = {};
function icon(name, template, fallback) {
  if (!ICONS[name]) {
    var img = $.NSImage.alloc.initWithContentsOfFile(STATIC + name + '.png');
    if (img.isNil()) img = $.NSImage.imageWithSystemSymbolNameAccessibilityDescription(fallback, 'cADRe');
    else img.size = $.NSMakeSize(18, 18);
    if (!img.isNil()) img.template = template;
    ICONS[name] = img;
  }
  return ICONS[name];
}

function render() {
  var snap = state.snap, c = (snap && snap.counts) || {};
  var working = c.working || 0, waiting = c.waiting || 0, isPaused = paused();
  var btn = item.button;

  // icon: mark = watching, amber = an agent waits for you, hollow and dimmed = dashboard down
  // or hooks paused; count = busy agents
  var off = !snap || isPaused;
  var name = off ? 'menubar-off' : waiting ? 'menubar-waiting' : 'menubar';
  btn.image = off ? icon(name, true, 'eye.slash')
            : waiting ? icon(name, false, 'exclamationmark.circle') : icon(name, true, 'eye');
  btn.appearsDisabled = off;
  btn.imagePosition = $.NSImageLeft;
  btn.title = snap && (working + waiting) ? ' ' + (working + waiting) : '';
  btn.toolTip = 'cADRe';

  if (state.open) return;
  var menu = item.menu;
  menu.removeAllItems;
  if (!snap) {
    add(menu, 'cADRe: dashboard not running' + (state.error ? ' (' + state.error + ')' : ''));
    add(menu, 'Start dashboard', 'start:');
  } else {
    var parts = [];
    ['working', 'waiting', 'idle', 'suspended'].forEach(function (k) { if (c[k]) parts.push(c[k] + ' ' + k); });
    add(menu, 'cADRe: ' + (parts.length ? parts.join(', ') : 'no agents running'));
    var live = (snap.agents || []).filter(function (a) { return a.status !== 'sleeping'; });
    live.slice(0, MAX_ROWS).forEach(function (a) {
      var doing = a.status === 'waiting' ? 'needs you' : a.last_tool || a.last_prompt || a.status;
      var mi = add(menu, (GLYPH[a.status] || '•') + '  ' + (a.display_name || a.agent) +
                   (a.project ? ' · ' + a.project : '') + '  ' + short(doing, 48), 'open:');
      mi.toolTip = a.status + (a.cwd ? '\n' + a.cwd : '');
      ((a.flags || []).slice(0, 2)).forEach(function (f) { add(menu, '      ! ' + short(f, 60)); });
    });
    if (live.length > MAX_ROWS) add(menu, '    … ' + (live.length - MAX_ROWS) + ' more', 'open:');
    if (c.sleeping) add(menu, c.sleeping + ' sleeping session' + (c.sleeping === 1 ? '' : 's'));
  }
  menu.addItem($.NSMenuItem.separatorItem);
  add(menu, 'Open dashboard', 'open:', !!snap);
  add(menu, isPaused ? 'Resume recording hook events' : 'Pause recording hook events', 'pause:');
  menu.addItem($.NSMenuItem.separatorItem);
  if (snap) add(menu, 'Stop dashboard', 'stop:');
  add(menu, 'Quit menu bar only', 'quiticon:');
  add(menu, 'Quit cADRe', 'quit:');
  if (DEBUG) {                                    // CADRE_MENUBAR_DEBUG=1: print what is shown
    var rows = [];
    for (var i = 0; i < menu.numberOfItems; i++) {
      var m = menu.itemAtIndex(i);
      rows.push(m.isSeparatorItem ? '----' : (m.isEnabled ? '  ' : '  (off) ') + m.title.js);
    }
    console.log('[icon ' + name + (btn.image.isNil() ? ' (missing)' : '') +
                ' title "' + btn.title.js + '"]\n' + rows.join('\n'));
  }
}

function poll() {
  if (state.inflight) return;
  state.inflight = true;
  var task = session.dataTaskWithURLCompletionHandler($.NSURL.URLWithString(BASE + '/api/snapshot'),
    function (data, resp, err) {                  // runs on the main queue (see `session`)
      state.inflight = false;
      var code = resp.isNil() ? 0 : Number(resp.statusCode);
      if (!err.isNil() || code !== 200) {
        state.snap = null;
        state.error = code ? 'HTTP ' + code : null;
      } else {
        try {
          state.snap = JSON.parse($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js);
          state.error = null;
        } catch (e) {
          state.snap = null;
          state.error = 'bad response';
        }
      }
      var got = freshAlerts(state.snap, alertMark);
      alertMark = got[1];
      if (got[0].length) {                        // a detection rule just flagged an agent
        var msg = alertText(got[0]);
        notify(msg[0], msg[1]);
        if (DEBUG) console.log('[alert] ' + msg[0] + ' | ' + msg[1].replace(/\n/g, ' | '));
      }
      render();
    });
  task.resume;
}

item.menu = $.NSMenu.alloc.init;
item.menu.autoenablesItems = false;
item.menu.delegate = target;
render();
poll();
$.NSTimer.scheduledTimerWithTimeIntervalTargetSelectorUserInfoRepeats(EVERY, target, 'tick:', $(), true);
app.run;
