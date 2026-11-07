/*
 * Stable depth ordering adapted from StarNet frontend/app/worldrenderer.js,
 * revision 74fcd5040ba0eb93c8f9fa07b2d0a9fcfddd1475.
 * https://github.com/androoAGI/starnet
 * All bridge geometry, crew artwork, and movement below are original Eva code.
 *
 * MIT License
 * Copyright (c) 2026 Andrew Sims
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
var EvaBridgeScene = (function() {
  'use strict';
  var state = {
    canvas: null, context: null, bodies: new Map(), active: false, stale: true,
    frame: null, last: 0, width: 0, height: 0, zoom: 1, panX: 0, panY: 0,
    selected: '', drag: null, observer: null
  };
  var motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var berths = [[265, 280], [695, 280], [180, 365], [780, 365], [265, 455], [695, 455]];
  var colors = ['#77d9c1', '#c5a5ff', '#f0bf76', '#89cfff', '#ed9fae', '#b1d18c'];

  function sortedItems(items) {
    return (items || []).map(function(item, index) { return { item: item, index: index }; })
      .sort(function(a, b) { return a.item.y - b.item.y || a.index - b.index; })
      .map(function(entry) { return entry.item; });
  }

  function hash(value) {
    var result = 2166136261;
    String(value).split('').forEach(function(char) { result = Math.imul(result ^ char.charCodeAt(0), 16777619); });
    return result >>> 0;
  }

  function shape(points, fill, stroke) {
    var ctx = state.context;
    ctx.beginPath();
    points.forEach(function(point, index) { if (index) ctx.lineTo(point[0], point[1]); else ctx.moveTo(point[0], point[1]); });
    ctx.closePath();
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke(); }
  }

  function ellipse(x, y, rx, ry, fill, stroke) {
    var ctx = state.context;
    ctx.beginPath(); ctx.ellipse(x, y, rx, ry, 0, 0, Math.PI * 2);
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.stroke(); }
  }

  function glow(x, y, radius, color) {
    var ctx = state.context;
    var light = ctx.createRadialGradient(x, y, 0, x, y, radius);
    light.addColorStop(0, color); light.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = light; ctx.fillRect(x - radius, y - radius, radius * 2, radius * 2);
  }

  function drawRoom(time) {
    var ctx = state.context;
    var wall = ctx.createLinearGradient(0, 0, 0, 210);
    wall.addColorStop(0, '#101a2c'); wall.addColorStop(1, '#263348');
    shape([[65, 65], [895, 65], [900, 210], [60, 210]], wall, '#516078');
    shape([[125, 80], [835, 80], [815, 175], [145, 175]], '#050d21', '#93b2cd');
    ctx.save();
    shape([[128, 82], [832, 82], [812, 172], [148, 172]]);
    ctx.clip();
    glow(685, 147, 230, '#395b7044'); glow(310, 95, 220, '#47386766');
    for (var star = 0; star < 95; star++) {
      var seed = hash('star-' + star);
      var x = 125 + ((seed % 710 + time * (1 + star % 3) * 0.0015) % 710);
      var y = 83 + (seed >>> 12) % 90;
      ctx.fillStyle = star % 4 ? '#bfd7e5' : '#e8bd90';
      ctx.globalAlpha = 0.35 + (star % 5) * 0.12;
      ctx.fillRect(x, y, star % 8 ? 1 : 2, 1);
    }
    ctx.globalAlpha = 1;
    var planet = ctx.createRadialGradient(670, 140, 3, 725, 195, 95);
    planet.addColorStop(0, '#aec1b9'); planet.addColorStop(0.45, '#57758d'); planet.addColorStop(1, '#15223c');
    ellipse(735, 190, 100, 100, planet);
    ctx.restore();
    shape([[110, 185], [850, 185], [950, 500], [10, 500]], '#253246', '#607086');
    ctx.save();
    shape([[110, 185], [850, 185], [950, 500], [10, 500]]);
    ctx.clip();
    for (var row = 0; row < 9; row++) {
      var y = 185 + row * row * 5;
      ctx.strokeStyle = '#52627744'; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(960, y); ctx.stroke();
    }
    for (var column = -7; column <= 7; column++) {
      ctx.beginPath(); ctx.moveTo(480 + column * 40, 185); ctx.lineTo(480 + column * 105, 540); ctx.stroke();
    }
    ellipse(480, 365, 215, 95, '#162538', '#6d8faa66');
    ellipse(480, 365, 206, 88, null, '#acc6de22');
    glow(480, 300, 190, '#67cce419');
    ctx.restore();
    shape([[65, 90], [110, 185], [40, 480], [8, 452]], '#152033', '#42546b');
    shape([[895, 90], [850, 185], [920, 480], [952, 452]], '#152033', '#42546b');
    for (var side = 0; side < 2; side++) {
      for (var panel = 0; panel < 5; panel++) {
        var px = side ? 866 + panel * 9 : 66 - panel * 9, py = 175 + panel * 50;
        shape([[px, py], [px + 25, py + 4], [px + 21, py + 33], [px - 4, py + 29]], '#35405a', '#74829a');
        ctx.fillStyle = colors[(panel + side) % colors.length];
        ctx.globalAlpha = 0.55 + Math.sin(time * 0.0005 + panel) * 0.08;
        ctx.fillRect(px + 2, py + 10, 15, 3); ctx.fillRect(px, py + 19, 15, 3);
        ctx.globalAlpha = 1;
        glow(px + 8, py + 15, 50, '#d7bb7820');
      }
    }
    ctx.fillStyle = '#b5c7d9'; ctx.font = '10px monospace';
    ctx.fillText('EVA / COMMAND', 411, 59);
    ctx.fillStyle = '#7089a6'; ctx.font = '8px monospace';
    ctx.fillText('AMBIENT EXPLORATION VIEW', 150, 99);
  }

  function consoleProp(x, y, working, time) {
    var ctx = state.context;
    ellipse(x, y + 18, 54, 14, '#03091677');
    shape([[x - 48, y - 4], [x + 40, y - 4], [x + 49, y + 13], [x - 39, y + 13]], '#526477', '#899caf');
    shape([[x - 39, y + 13], [x + 49, y + 13], [x + 44, y + 29], [x - 35, y + 29]], '#253345', '#53647a');
    shape([[x - 26, y - 39], [x + 28, y - 39], [x + 28, y - 8], [x - 26, y - 8]], '#101c29', '#87a6b9');
    ctx.fillStyle = working ? '#68cfac' : '#4e7791';
    for (var line = 0; line < 4; line++) {
      ctx.globalAlpha = working ? 0.7 + Math.sin(time * 0.003 + line) * 0.15 : 0.5;
      ctx.fillRect(x - 19, y - 33 + line * 6, 12 + (line * 9) % 29, 2);
    }
    ctx.globalAlpha = 1;
    if (working) glow(x, y - 17, 55, '#6ddca732');
  }

  function drawChair() {
    var ctx = state.context;
    ellipse(480, 418, 34, 12, '#0007');
    shape([[453, 386], [506, 386], [510, 416], [450, 416]], '#17273a', '#7b8da4');
    shape([[454, 400], [506, 400], [501, 428], [459, 428]], '#394963', '#9caab6');
    ctx.fillStyle = '#93b8cd'; ctx.fillRect(446, 399, 10, 5); ctx.fillRect(504, 399, 10, 5);
  }

  function drawTable(time) {
    var ctx = state.context;
    ellipse(480, 315, 67, 24, '#050b1766');
    shape([[455, 284], [505, 284], [499, 312], [461, 312]], '#21364c', '#55738c');
    ellipse(480, 285, 64, 24, '#34566a', '#8badc4');
    ellipse(480, 280, 57, 20, '#102438', '#5bbed5');
    ctx.strokeStyle = '#9ce6f275'; ctx.lineWidth = 1;
    ellipse(480, 259, 24, 24, null, '#9ce6f275');
    for (var line = 0; line < 4; line++) {
      ctx.beginPath();
      ctx.ellipse(480, 259, 8 + line * 5, 24, time * 0.0001 + line, 0, Math.PI * 2); ctx.stroke();
    }
    glow(480, 265, 65, '#80cfeb24');
  }

  function destination(body, time) {
    if (body.agent.kind === 'eva') {
      if (motion.matches || state.stale) return [480, 396];
      var phase = Math.floor(time / 14000) % 3;
      return [[480, 396], [440, 366], [520, 366]][phase];
    }
    if (body.agent.status === 'running' || body.agent.status === 'starting' || body.agent.status === 'steering') {
      var berth = berths[body.slot % berths.length];
      return [berth[0] + Math.floor(body.slot / berths.length) * 18, berth[1] + 42];
    }
    var angle = body.slot * 2.4;
    if (/^(done|error|cancelled)$/.test(body.agent.status)) return [370 + (body.slot % 5) * 55, 465];
    return [480 + Math.cos(angle) * 120, 370 + Math.sin(angle) * 55];
  }

  function drawBody(body, time) {
    var ctx = state.context, x = body.x, y = body.y;
    var working = !state.stale && body.agent.status === 'running' && !body.walking;
    var walking = !state.stale && !motion.matches && body.walking;
    var phase = motion.matches || state.stale ? 0 : time * 0.008 + body.seed;
    var step = walking ? Math.sin(phase) * 3 : 0;
    var bob = walking ? Math.abs(Math.sin(phase)) * 1.2 : (motion.matches || state.stale ? 0 : Math.sin(phase * 0.13) * 0.5);
    ellipse(x, y + 1, 18, 6, '#02081288');
    if (working) ellipse(x, y + 1, 23, 8, null, body.color + '77');
    if (state.selected === body.id) ellipse(x, y + 2, 26, 10, null, '#ffce89');
    ctx.save(); ctx.translate(Math.round(x), Math.round(y - bob)); ctx.scale(1.2, 1.2);
    if (body.facing < 0) ctx.scale(-1, 1);
    ctx.fillStyle = '#102030';
    ctx.fillRect(-11, -12 + step, 8, 12); ctx.fillRect(3, -12 - step, 8, 12);
    ctx.fillStyle = '#87a2ae'; ctx.fillRect(-12, -3 + step, 10, 4); ctx.fillRect(2, -3 - step, 10, 4);
    ctx.fillStyle = body.color; ctx.fillRect(-13, -31, 26, 22);
    ctx.fillStyle = '#182b3b'; ctx.fillRect(-10, -18, 20, 8);
    ctx.fillStyle = '#b0dce4'; ctx.fillRect(-3, -26, 6, 5);
    var hand = working && !motion.matches ? Math.sin(phase * 1.5) * 2 : step;
    ctx.fillStyle = body.color; ctx.fillRect(-19, -29 + hand, 6, 17); ctx.fillRect(13, -29 - hand, 6, 17);
    ctx.fillStyle = '#c5d8da'; ctx.fillRect(-19, -14 + hand, 6, 4); ctx.fillRect(13, -14 - hand, 6, 4);
    ctx.fillStyle = '#36485a'; ctx.fillRect(-13, -49, 26, 19);
    ctx.fillStyle = body.color; ctx.fillRect(-11, -49, 22, 3);
    ctx.fillStyle = '#082332'; ctx.fillRect(-11, -44, 22, 9);
    ctx.fillStyle = '#d3f5f5'; ctx.fillRect(-7, -42, 4, 3); ctx.fillRect(4, -42, 4, 3);
    ctx.fillStyle = '#799eb2'; ctx.fillRect(13, -43, 3, 10);
    if (body.agent.kind === 'eva') { ctx.fillStyle = '#ffd08a'; ctx.fillRect(-7, -54, 14, 3); }
    ctx.restore();
    var widget = document.querySelector('#agentsFloorGroups [data-agent-id="' + CSS.escape(body.id) + '"]');
    if (widget) {
      var scale = Math.min(state.width / 960, state.height / 540) * state.zoom;
      widget.style.left = (state.width / 2 + (x - 480) * scale + state.panX) + 'px';
      widget.style.top = (state.height / 2 + (y - 270) * scale + state.panY) + 'px';
      widget.style.zIndex = String(Math.round(y));
      widget.dataset.pose = walking ? 'walking' : (working ? 'working' : 'resting');
      widget.dataset.sceneX = x.toFixed(2); widget.dataset.sceneY = y.toFixed(2);
    }
  }

  function draw(now) {
    if (!state.context || !state.width || !state.height) return;
    var dt = state.last ? Math.min(0.06, (now - state.last) / 1000) : 0;
    state.last = now;
    var time = motion.matches ? 0 : now;
    var ctx = state.context;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#070f1d'; ctx.fillRect(0, 0, state.width, state.height);
    var scale = Math.min(state.width / 960, state.height / 540) * state.zoom;
    ctx.translate(state.width / 2 + state.panX, state.height / 2 + state.panY);
    ctx.scale(scale, scale); ctx.translate(-480, -270);
    drawRoom(time);
    var items = [];
    berths.forEach(function(berth, index) {
      var working = !state.stale && Array.from(state.bodies.values()).some(function(body) {
        return body.slot === index && body.agent.status === 'running';
      });
      items.push({ y: berth[1] + 15, draw: function() { consoleProp(berth[0], berth[1] - 12, working, time); } });
    });
    items.push({ y: 304, draw: function() { drawTable(time); } }, { y: 420, draw: drawChair });
    state.bodies.forEach(function(body) {
      if (!state.stale) {
        var target = destination(body, time), dx = target[0] - body.x, dy = target[1] - body.y;
        var distance = Math.hypot(dx, dy);
        body.walking = !motion.matches && distance > 2;
        if (motion.matches) { body.x = target[0]; body.y = target[1]; }
        else if (distance > 0.1) {
          var step = Math.min(distance, dt * 100);
          body.x += dx / distance * step; body.y += dy / distance * step;
          if (Math.abs(dx) > 1) body.facing = dx < 0 ? -1 : 1;
        }
      } else body.walking = false;
      items.push({ y: body.y, draw: function() { drawBody(body, time); } });
    });
    sortedItems(items).forEach(function(item) { item.draw(); });
    state.canvas.dataset.motion = motion.matches ? 'still' : 'ambient';
    state.canvas.dataset.crewCount = String(state.bodies.size);
  }

  function schedule(now) {
    state.frame = null;
    if (!state.active || document.hidden) return;
    if (!state.last || now - state.last >= 30) draw(now);
    if (!motion.matches) state.frame = requestAnimationFrame(schedule);
  }

  function resize() {
    if (!state.canvas) return;
    var rect = state.canvas.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (rect.width && (!state.width || (rect.width < 600) !== (state.width < 600))) {
      state.zoom = rect.width < 600 ? 1.65 : 1.1; state.panX = 0; state.panY = 0;
    }
    state.width = rect.width; state.height = rect.height;
    state.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    state.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    draw(performance.now());
  }

  function start() {
    if (state.frame) cancelAnimationFrame(state.frame);
    state.frame = null; state.last = 0;
    if (state.active && !document.hidden) state.frame = requestAnimationFrame(schedule);
  }

  function camera(zoom, dx, dy) {
    state.zoom = Math.max(0.8, Math.min(2.4, zoom));
    state.panX = Math.max(-state.width, Math.min(state.width, state.panX + dx));
    state.panY = Math.max(-state.height, Math.min(state.height, state.panY + dy));
    draw(performance.now());
  }

  function ensure() {
    var canvas = document.getElementById('bridgeSceneCanvas');
    if (!canvas || canvas === state.canvas) return;
    if (state.observer) state.observer.disconnect();
    state.canvas = canvas; state.context = canvas.getContext('2d');
    if (!state.context) return;
    state.observer = new ResizeObserver(resize); state.observer.observe(canvas);
    [['bridgeZoomIn', 0.2], ['bridgeZoomOut', -0.2], ['bridgeRecenter', 0]].forEach(function(pair) {
      var button = document.getElementById(pair[0]);
      if (button) button.addEventListener('click', function() {
        if (!pair[1]) { state.panX = 0; state.panY = 0; camera(state.width < 600 ? 1.65 : 1.1, 0, 0); }
        else camera(state.zoom + pair[1], 0, 0);
      });
    });
    canvas.addEventListener('pointerdown', function(event) {
      if (event.button !== 0) return;
      state.drag = { x: event.clientX, y: event.clientY }; canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', function(event) {
      if (!state.drag) return;
      camera(state.zoom, event.clientX - state.drag.x, event.clientY - state.drag.y);
      state.drag = { x: event.clientX, y: event.clientY };
    });
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(function(name) {
      canvas.addEventListener(name, function() { state.drag = null; });
    });
    canvas.addEventListener('keydown', function(event) {
      if (event.key === 'Home') { state.panX = 0; state.panY = 0; camera(state.width < 600 ? 1.65 : 1.1, 0, 0); }
      else if (event.key === '+' || event.key === '=') camera(state.zoom + 0.2, 0, 0);
      else if (event.key === '-') camera(state.zoom - 0.2, 0, 0);
      else if (event.key === 'ArrowLeft') camera(state.zoom, 30, 0);
      else if (event.key === 'ArrowRight') camera(state.zoom, -30, 0);
      else if (event.key === 'ArrowUp') camera(state.zoom, 0, 30);
      else if (event.key === 'ArrowDown') camera(state.zoom, 0, -30);
      else return;
      event.preventDefault();
    });
    resize();
  }

  function update(agents, options) {
    ensure();
    state.stale = !!options.stale; state.selected = options.selectedId || '';
    var ids = new Set(agents.map(function(agent) { return agent.id; }));
    state.bodies.forEach(function(body, id) { if (!ids.has(id)) state.bodies.delete(id); });
    var slots = new Set();
    state.bodies.forEach(function(body) { if (body.agent.kind !== 'eva') slots.add(body.slot); });
    agents.forEach(function(agent) {
      var body = state.bodies.get(agent.id);
      if (!body) {
        var slot = 0; while (slots.has(slot)) slot++;
        if (agent.kind !== 'eva') slots.add(slot);
        body = { id: agent.id, agent: agent, slot: slot, seed: hash(agent.id), x: 810 - slot * 15, y: 470,
          color: agent.kind === 'eva' ? '#b7a1ea' : colors[hash(agent.id) % colors.length], facing: -1, walking: false };
        var target = destination(body, performance.now());
        if (motion.matches || state.stale || agent.kind === 'eva') { body.x = target[0]; body.y = target[1]; }
        state.bodies.set(agent.id, body);
      }
      body.agent = agent;
    });
    draw(performance.now());
  }

  function setActive(active) {
    state.active = !!active; ensure(); start();
  }

  motion.addEventListener('change', function() { draw(performance.now()); start(); });
  document.addEventListener('visibilitychange', start);
  return { update: update, setActive: setActive };
})();
