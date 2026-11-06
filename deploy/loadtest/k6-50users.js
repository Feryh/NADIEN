// NADIEN load test: 50 concurrent users browsing the heaviest read views.
//
//   k6 run -e BASE_URL=https://nadien.example.co.id -e NADIEN_USER=loadtest -e NADIEN_PASSWORD=... deploy/loadtest/k6-50users.js
//
// Options (all via -e):
//   VUS=50 DURATION=5m      constant users and how long
//   THINK_MIN=3 THINK_MAX=8 seconds between clicks; 3-8 is a person reading, 0.5-2 is a stress test
//   RAMP=30s                time to reach VUS, so the first cold-cache burst is measured apart from steady load
//   INVALIDATE_EVERY=0      >0: one extra user writes every N seconds, which drops the read-model cache on all workers
//
// Use a dedicated account (viewer, or engineer with INVALIDATE_EVERY). Run it against staging, or production outside
// working hours.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const VUS = Number(__ENV.VUS || 50);
const DURATION = __ENV.DURATION || '5m';
const RAMP = __ENV.RAMP || '30s';
const THINK_MIN = Number(__ENV.THINK_MIN || 3);
const THINK_MAX = Number(__ENV.THINK_MAX || 8);
const INVALIDATE_EVERY = Number(__ENV.INVALIDATE_EVERY || 0);

const errors = new Rate('app_errors');
const byView = {};
const VIEWS = ['dashboard', 'topology_area', 'topology_pop', 'topology_device', 'hotspots', 'capacity_nodes',
  'ports_summary', 'ports_devices', 'devices_list', 'search', 'signature'];
for (const v of VIEWS) byView[v] = new Trend(`view_${v}`, true);

const scenarios = {
  users: {
    executor: 'ramping-vus',
    stages: [{ duration: RAMP, target: VUS }, { duration: DURATION, target: VUS }],
    gracefulRampDown: '10s',
    exec: 'browse',
  },
};
if (INVALIDATE_EVERY > 0) {
  scenarios.writer = { executor: 'constant-vus', vus: 1, duration: DURATION, startTime: RAMP, exec: 'write' };
}

export const options = {
  scenarios,
  noCookiesReset: true, // k6 empties the cookie jar every iteration by default, which ends the login session
  thresholds: {
    http_req_failed: ['rate<0.01'],
    app_errors: ['rate<0.01'],
    // Steady load only: the targets a person notices.
    view_dashboard: ['p(95)<1500'],
    view_topology_area: ['p(95)<1500'],
    view_topology_pop: ['p(95)<1500'],
    view_topology_device: ['p(95)<1500'],
    view_hotspots: ['p(95)<2000'],
    view_ports_summary: ['p(95)<2000'],
  },
};

function login() {
  if (!__ENV.NADIEN_USER) return;
  const res = http.post(`${BASE}/api/auth/login`,
    JSON.stringify({ username: __ENV.NADIEN_USER, password: __ENV.NADIEN_PASSWORD }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'login' } });
  if (res.status !== 200) throw new Error(`login failed: HTTP ${res.status}`);
}

export function setup() {
  login();
  const area = http.get(`${BASE}/api/topology/view?level=area`, { tags: { name: 'setup' } });
  if (area.status !== 200) throw new Error(`cannot read the area view: HTTP ${area.status}`);
  const areas = area.json('nodes').map((n) => n.label);
  const pops = [];
  for (const name of areas.slice(0, 6)) {
    const view = http.get(`${BASE}/api/topology/view?level=pop&area=${encodeURIComponent(name)}`, { tags: { name: 'setup' } });
    if (view.status === 200) pops.push(...view.json('nodes').filter((n) => n.kind === 'pop').map((n) => n.code));
  }
  return { areas, pops };
}

function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

function get(view, path) {
  const res = http.get(`${BASE}${path}`, { tags: { name: view } });
  const ok = check(res, { 'status 200': (r) => r.status === 200 });
  errors.add(!ok);
  byView[view].add(res.timings.duration);
  return res;
}

let loggedIn = false;

export function browse(data) {
  if (!loggedIn) {
    login(); // one session per user; k6 keeps the cookie for this VU
    loggedIn = true;
  }
  const roll = Math.random() * 100;
  if (roll < 20) get('dashboard', '/api/dashboard/summary');
  else if (roll < 35) get('topology_area', '/api/topology/view?level=area');
  else if (roll < 50) get('topology_pop', `/api/topology/view?level=pop&area=${encodeURIComponent(pick(data.areas))}`);
  else if (roll < 65) get('topology_device', `/api/topology/view?level=device&pop=${encodeURIComponent(pick(data.pops))}`);
  else if (roll < 75) get('hotspots', '/api/capacity/hotspots');
  else if (roll < 82) get('capacity_nodes', '/api/capacity/nodes?level=pop');
  else if (roll < 89) get('ports_summary', '/api/inventory/ports/summary');
  else if (roll < 94) get('ports_devices', '/api/inventory/ports/devices?limit=200');
  else if (roll < 97) get('devices_list', '/api/devices?limit=50');
  else get('search', `/api/interfaces?q=${pick(['xgei', 'OLT', 'Eth-Trunk'])}&limit=50`);
  get('signature', '/api/topology/signature'); // the topology page polls this
  sleep(THINK_MIN + Math.random() * (THINK_MAX - THINK_MIN));
}

export function write() {
  if (!loggedIn) {
    login();
    loggedIn = true;
  }
  // Saves no positions, so no data changes, yet it counts as a write: the read-model cache drops on every worker.
  // The account needs the engineer role for this.
  const res = http.put(`${BASE}/api/topology/positions`, '[]',
    { headers: { 'Content-Type': 'application/json' }, tags: { name: 'write' } });
  check(res, { 'write 204': (r) => r.status === 204 });
  sleep(INVALIDATE_EVERY);
}
