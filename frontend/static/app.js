"use strict";

const app = document.getElementById("app");

// --------------------------- API helpers ---------------------------
async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

const getJSON = (p) => api(p);
const postJSON = (p, body) =>
  api(p, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
const del = (p) => api(p, { method: "DELETE" });

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function esc(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// --------------------------- Router ---------------------------
const routes = [
  [/^\/upload$/, viewUpload],
  [/^\/photos$/, viewPhotos],
  [/^\/photo\/(\d+)$/, viewPhotoDetail],
  [/^\/people$/, viewPeople],
  [/^\/person\/(\d+)$/, viewPerson],
];

function router() {
  const hash = location.hash.replace(/^#/, "") || "/photos";
  for (const [re, handler] of routes) {
    const m = hash.match(re);
    if (m) {
      handler(...m.slice(1));
      return;
    }
  }
  location.hash = "#/photos";
}

window.addEventListener("hashchange", router);
window.addEventListener("load", router);

function setView(node) {
  app.innerHTML = "";
  app.appendChild(node);
}

// --------------------------- Upload ---------------------------
function viewUpload() {
  const view = el(`
    <div>
      <h1>Upload photos</h1>
      <div class="uploader">
        <p class="muted">Take photos with your camera, then pick them here.<br/>Full resolution is preserved.</p>
        <input type="file" accept="image/*" multiple id="fileInput" />
        <button id="uploadBtn" disabled>Upload</button>
        <div class="upload-list" id="uploadList"></div>
      </div>
    </div>
  `);
  const input = view.querySelector("#fileInput");
  const btn = view.querySelector("#uploadBtn");
  const list = view.querySelector("#uploadList");

  input.addEventListener("change", () => {
    btn.disabled = input.files.length === 0;
  });

  btn.addEventListener("click", async () => {
    if (!input.files.length) return;
    btn.disabled = true;
    btn.textContent = "Uploading…";
    list.innerHTML = "";
    const fd = new FormData();
    for (const f of input.files) fd.append("files", f);
    try {
      const data = await api("/api/upload", { method: "POST", body: fd });
      for (const r of data.results) {
        list.appendChild(
          el(
            `<div class="upload-row"><span>${esc(r.filename)}</span><span class="tag ${r.status}">${r.status}</span></div>`
          )
        );
      }
      btn.textContent = "Done — upload more";
      input.value = "";
    } catch (e) {
      list.appendChild(el(`<div class="upload-row"><span>Error</span><span>${esc(e.message)}</span></div>`));
      btn.textContent = "Upload";
    } finally {
      btn.disabled = false;
    }
  });

  setView(view);
}

// --------------------------- Photos grid ---------------------------
async function viewPhotos() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const filter = params.get("filter") || "all";

  const view = el(`
    <div>
      <h1>Photos</h1>
      <div class="toolbar">
        <a href="#/photos?filter=all" class="chip ${filter === "all" ? "active" : ""}">All</a>
        <a href="#/photos?filter=untagged" class="chip ${filter === "untagged" ? "active" : ""}">Needs tagging</a>
      </div>
      <div id="gridWrap"></div>
    </div>
  `);
  setView(view);

  const wrap = view.querySelector("#gridWrap");
  try {
    const { photos } = await getJSON(`/api/photos?filter=${filter}`);
    if (!photos.length) {
      wrap.appendChild(
        el(`<div class="empty">No photos yet. <a href="#/upload">Upload some</a>.</div>`)
      );
      return;
    }
    const grid = el(`<div class="grid"></div>`);
    for (const p of photos) {
      let badge = "";
      if (!p.processed) badge = `<span class="badge">detecting…</span>`;
      else if (p.untagged_count > 0)
        badge = `<span class="badge warn">${p.untagged_count} to tag</span>`;
      else if (p.face_count > 0) badge = `<span class="badge ok">tagged</span>`;
      else badge = `<span class="badge">no faces</span>`;
      const card = el(
        `<div class="card">${badge}<img loading="lazy" src="/media/${esc(p.display_filename || p.filename)}" alt=""/></div>`
      );
      card.addEventListener("click", () => (location.hash = `#/photo/${p.id}`));
      grid.appendChild(card);
    }
    wrap.appendChild(grid);
  } catch (e) {
    wrap.appendChild(el(`<div class="empty">${esc(e.message)}</div>`));
  }
}

// --------------------------- Photo detail ---------------------------
async function viewPhotoDetail(id) {
  const view = el(`
    <div>
      <a class="back" href="#/photos">← Back to photos</a>
      <div class="detail">
        <div class="stage-col">
          <div class="stage" id="stage"></div>
          <div class="stage-tools" id="stageTools"></div>
          <div id="descWrap"></div>
        </div>
        <div class="side" id="side"></div>
      </div>
    </div>
  `);
  setView(view);

  const stage = view.querySelector("#stage");
  const side = view.querySelector("#side");
  const descWrap = view.querySelector("#descWrap");
  const stageTools = view.querySelector("#stageTools");
  let photo, persons;
  try {
    [photo, persons] = await Promise.all([
      getJSON(`/api/photos/${id}`),
      getJSON(`/api/persons`),
    ]);
  } catch (e) {
    stage.appendChild(el(`<div class="empty">${esc(e.message)}</div>`));
    return;
  }

  const img = el(`<img src="/media/${esc(photo.display_filename || photo.filename)}" alt=""/>`);
  stage.appendChild(img);

  const curW = () => photo.width || 1;
  const curH = () => photo.height || 1;

  // Description editor (persists across face reloads).
  const descBox = el(`
    <div class="desc-box">
      <label class="desc-label">Description</label>
      <textarea class="desc-input" rows="3" placeholder="Add a note about this photo…">${esc(photo.description || "")}</textarea>
      <div class="desc-actions">
        <button class="save desc-save">Save</button>
        <span class="desc-status muted"></span>
      </div>
    </div>
  `);
  descWrap.appendChild(descBox);
  const descInput = descBox.querySelector(".desc-input");
  const descStatus = descBox.querySelector(".desc-status");
  descBox.querySelector(".desc-save").addEventListener("click", async () => {
    descStatus.textContent = "Saving…";
    try {
      await postJSON(`/api/photos/${id}/description`, {
        description: descInput.value,
      });
      photo.description = descInput.value.trim();
      descStatus.textContent = "Saved";
    } catch (e) {
      descStatus.textContent = e.message;
    }
  });

  // datalist for autocomplete
  const datalistId = "personsList";
  const datalist = el(`<datalist id="${datalistId}"></datalist>`);
  for (const p of persons.persons) {
    datalist.appendChild(el(`<option value="${esc(p.name)}"></option>`));
  }
  side.appendChild(datalist);

  function pct(v, total) {
    return (v / total) * 100 + "%";
  }

  function render() {
    // clear boxes
    stage.querySelectorAll(".facebox").forEach((n) => n.remove());
    side.querySelectorAll(".face-item, .side-head, .empty").forEach((n) => n.remove());

    if (!photo.processed) {
      side.appendChild(el(`<div class="empty">Detecting faces…<br/><span class="muted">refresh in a moment</span></div>`));
      return;
    }
    if (!photo.faces.length) {
      side.appendChild(el(`<div class="empty">No faces detected.</div>`));
      return;
    }

    side.appendChild(el(`<div class="side-head"><strong>${photo.faces.length} face(s)</strong></div>`));

    photo.faces.forEach((f, i) => {
      const named = f.person_id != null;
      const box = el(
        `<div class="facebox ${named ? "named" : ""}" data-fid="${f.id}"
           style="left:${pct(f.x, curW())};top:${pct(f.y, curH())};width:${pct(f.w, curW())};height:${pct(f.h, curH())}">
           <span class="num">${i + 1}</span>
           ${named ? `<span class="label">${esc(f.person_name)}</span>` : ""}
         </div>`
      );
      stage.appendChild(box);

      const suggested = !named && f.suggested_person_name != null;
      const item = el(`
        <div class="face-item ${named ? "named" : ""}" data-fid="${f.id}">
          <div><span class="num-pill">${i + 1}</span>
            <span class="muted"> confidence ${(f.confidence * 100).toFixed(0)}%</span></div>
          ${suggested ? `<div class="suggestion">Looks like <strong>${esc(f.suggested_person_name)}</strong> (${(f.suggested_score * 100).toFixed(0)}%) <button class="confirm">Confirm</button></div>` : ""}
          <div class="row">
            <input type="text" list="${datalistId}" placeholder="Type a name…"
              value="${named ? esc(f.person_name) : ""}" />
            <button class="save">Save</button>
          </div>
          <div class="row">
            ${named ? `<button class="secondary clear">Remove name</button>` : ""}
            <button class="danger notface">Not a face</button>
          </div>
        </div>
      `);
      side.appendChild(item);

      const confirmBtn = item.querySelector(".confirm");
      if (confirmBtn)
        confirmBtn.addEventListener("click", async () => {
          try {
            await postJSON(`/api/faces/${f.id}/assign`, {
              person_id: f.suggested_person_id,
            });
            await reload();
          } catch (e) {
            alert(e.message);
          }
        });

      const input = item.querySelector("input");
      const highlight = (on) => {
        box.classList.toggle("active", on);
        if (on) box.scrollIntoView({ block: "nearest" });
      };
      box.addEventListener("click", () => {
        input.focus();
        item.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      input.addEventListener("focus", () => highlight(true));
      input.addEventListener("blur", () => highlight(false));

      const save = async () => {
        const name = input.value.trim();
        if (!name) return;
        try {
          await postJSON(`/api/faces/${f.id}/assign`, { name });
          await reload();
        } catch (e) {
          alert(e.message);
        }
      };
      item.querySelector(".save").addEventListener("click", save);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") save();
      });

      const clearBtn = item.querySelector(".clear");
      if (clearBtn)
        clearBtn.addEventListener("click", async () => {
          await postJSON(`/api/faces/${f.id}/unassign`, {});
          await reload();
        });

      item.querySelector(".notface").addEventListener("click", async () => {
        if (!confirm("Remove this detection?")) return;
        await del(`/api/faces/${f.id}`);
        await reload();
      });
    });
  }

  async function reload() {
    [photo, persons] = await Promise.all([
      getJSON(`/api/photos/${id}`),
      getJSON(`/api/persons`),
    ]);
    datalist.innerHTML = "";
    for (const p of persons.persons)
      datalist.appendChild(el(`<option value="${esc(p.name)}"></option>`));
    const src = `/media/${esc(photo.display_filename || photo.filename)}`;
    if (!img.src.endsWith(src)) img.src = src;
    render();
    ensurePolling();
  }

  // --- Manual face box drawing ---
  const drawBtn = el(`<button class="secondary draw-toggle">+ Add face box</button>`);
  const drawHint = el(`<span class="muted draw-hint" hidden>Drag on the photo to mark a face.</span>`);
  stageTools.appendChild(drawBtn);
  stageTools.appendChild(drawHint);

  // --- Perspective correction / crop ---
  const adjustBtn = el(`<button class="secondary">✂ Adjust / Crop</button>`);
  stageTools.appendChild(adjustBtn);
  adjustBtn.addEventListener("click", () => openAdjustEditor(id, reload));
  if (photo.has_edit) {
    const resetBtn = el(`<button class="secondary">↩ Revert to original</button>`);
    stageTools.appendChild(resetBtn);
    resetBtn.addEventListener("click", async () => {
      if (!confirm("Discard the crop and go back to the original photo?")) return;
      try {
        await postJSON(`/api/photos/${id}/warp/reset`, {});
        await reload();
      } catch (e) {
        alert(e.message);
      }
    });
  }

  let drawMode = false;
  function setDrawMode(on) {
    drawMode = on;
    stage.classList.toggle("drawing", on);
    drawBtn.classList.toggle("active", on);
    drawBtn.textContent = on ? "Cancel" : "+ Add face box";
    drawHint.hidden = !on;
  }
  drawBtn.addEventListener("click", () => setDrawMode(!drawMode));

  let draft = null;
  let startX = 0;
  let startY = 0;

  function frac(e) {
    const rect = stage.getBoundingClientRect();
    const fx = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const fy = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
    return [fx, fy];
  }

  stage.addEventListener("pointerdown", (e) => {
    if (!drawMode) return;
    e.preventDefault();
    [startX, startY] = frac(e);
    draft = el(
      `<div class="facebox draft" style="left:${startX * 100}%;top:${startY * 100}%;width:0;height:0"></div>`
    );
    stage.appendChild(draft);
    stage.setPointerCapture(e.pointerId);
  });

  stage.addEventListener("pointermove", (e) => {
    if (!draft) return;
    const [fx, fy] = frac(e);
    draft.style.left = Math.min(fx, startX) * 100 + "%";
    draft.style.top = Math.min(fy, startY) * 100 + "%";
    draft.style.width = Math.abs(fx - startX) * 100 + "%";
    draft.style.height = Math.abs(fy - startY) * 100 + "%";
  });

  async function finishDraft(e) {
    if (!draft) return;
    const [fx, fy] = frac(e);
    const left = Math.min(fx, startX);
    const top = Math.min(fy, startY);
    const w = Math.abs(fx - startX);
    const h = Math.abs(fy - startY);
    draft.remove();
    draft = null;
    setDrawMode(false);
    if (w < 0.01 || h < 0.01) return; // ignore accidental clicks
    try {
      await postJSON(`/api/photos/${id}/faces`, {
        x: left * curW(),
        y: top * curH(),
        w: w * curW(),
        h: h * curH(),
      });
      await reload();
    } catch (err) {
      alert(err.message);
    }
  }

  stage.addEventListener("pointerup", finishDraft);
  stage.addEventListener("pointercancel", () => {
    if (draft) {
      draft.remove();
      draft = null;
    }
    setDrawMode(false);
  });

  if (img.complete) render();
  else img.addEventListener("load", render);

  // Keep the face list the same height as the image so it scrolls beside it.
  const isNarrow = () => window.matchMedia("(max-width: 760px)").matches;
  function syncSideHeight() {
    if (isNarrow()) {
      side.style.maxHeight = "";
      side.style.overflowY = "";
      return;
    }
    const h = stage.getBoundingClientRect().height;
    if (h > 0) {
      side.style.maxHeight = h + "px";
      side.style.overflowY = "auto";
    }
  }
  if (img.complete) syncSideHeight();
  else img.addEventListener("load", syncSideHeight);
  const onResize = () => {
    if (!document.body.contains(view)) {
      window.removeEventListener("resize", onResize);
      return;
    }
    syncSideHeight();
  };
  window.addEventListener("resize", onResize);

  // Poll while detection is pending (also restarts after a re-crop).
  let pollTimer = null;
  function ensurePolling() {
    if (photo.processed) {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      return;
    }
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      if (!document.body.contains(view)) {
        clearInterval(pollTimer);
        pollTimer = null;
        return;
      }
      await reload();
    }, 2500);
  }
  ensurePolling();
}

// --------------------- Perspective correction editor ---------------------
async function openAdjustEditor(id, onDone) {
  let info;
  try {
    info = await getJSON(`/api/photos/${id}/adjust`);
  } catch (e) {
    alert(e.message);
    return;
  }

  const DEFAULT_QUAD = [
    [0.05, 0.05],
    [0.95, 0.05],
    [0.95, 0.95],
    [0.05, 0.95],
  ];
  const clamp = (v) => Math.min(1, Math.max(0, v));
  const rotateCW = (cs) => cs.map((c) => ({ x: 1 - c.y, y: c.x }));

  const overlay = el(`
    <div class="editor-overlay">
      <div class="editor-panel">
        <div class="editor-head">
          <strong>Adjust photo</strong>
          <span class="muted">Drag the 4 corners onto the edges of the real photo.</span>
        </div>
        <div class="editor-canvas-wrap"><canvas class="editor-canvas"></canvas></div>
        <div class="editor-tools">
          <button class="secondary" data-act="rotate">⟳ Rotate 90°</button>
          <button class="secondary" data-act="auto">⤢ Auto-detect edges</button>
          <span class="editor-spacer"></span>
          <button class="secondary" data-act="cancel">Cancel</button>
          <button class="save" data-act="apply">Apply</button>
        </div>
        <div class="editor-status muted"></div>
      </div>
    </div>
  `);
  document.body.appendChild(overlay);

  const canvas = overlay.querySelector(".editor-canvas");
  const ctx = canvas.getContext("2d");
  const status = overlay.querySelector(".editor-status");

  const ow = info.original_width || 1;
  const oh = info.original_height || 1;

  let rotation = 0;
  let corners = (info.suggestion || DEFAULT_QUAD).map(([x, y]) => ({ x, y }));
  if (info.current && Array.isArray(info.current.points) && info.current.points.length === 4) {
    rotation = (((info.current.rotation || 0) % 360) + 360) % 360;
    corners = info.current.points.map(([x, y]) => ({ x, y }));
  }

  const dispDims = () => (rotation % 180 === 0 ? [ow, oh] : [oh, ow]);
  const view = { cw: 0, ch: 0 };

  function layout() {
    const [dw, dh] = dispDims();
    const maxW = Math.min(window.innerWidth - 80, 900);
    const maxH = window.innerHeight - 220;
    const scale = Math.min(maxW / dw, maxH / dh, 1);
    view.cw = Math.max(1, Math.round(dw * scale));
    view.ch = Math.max(1, Math.round(dh * scale));
    canvas.width = view.cw;
    canvas.height = view.ch;
  }

  function draw() {
    ctx.clearRect(0, 0, view.cw, view.ch);
    ctx.save();
    ctx.translate(view.cw / 2, view.ch / 2);
    ctx.rotate((rotation * Math.PI) / 180);
    const scale = view.cw / dispDims()[0];
    ctx.drawImage(img, (-ow * scale) / 2, (-oh * scale) / 2, ow * scale, oh * scale);
    ctx.restore();

    const pts = corners.map((c) => ({ x: c.x * view.cw, y: c.y * view.ch }));
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#38bdf8";
    ctx.fillStyle = "rgba(56,189,248,0.12)";
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    pts.forEach((p) => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 9, 0, Math.PI * 2);
      ctx.fillStyle = "#0ea5e9";
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#fff";
      ctx.stroke();
    });
  }

  const rerender = () => {
    layout();
    draw();
  };

  const img = new Image();
  img.onload = rerender;
  img.onerror = () => {
    status.textContent = "Could not load the original image.";
  };
  img.src = `/media/${esc(info.filename || "")}`;

  function canvasPoint(e) {
    const rect = canvas.getBoundingClientRect();
    const px = ((e.clientX - rect.left) / rect.width) * view.cw;
    const py = ((e.clientY - rect.top) / rect.height) * view.ch;
    return { px, py };
  }

  let dragIdx = -1;
  canvas.addEventListener("pointerdown", (e) => {
    const { px, py } = canvasPoint(e);
    let best = -1;
    let bestD = 22 * 22;
    corners.forEach((c, i) => {
      const dx = c.x * view.cw - px;
      const dy = c.y * view.ch - py;
      const d = dx * dx + dy * dy;
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    });
    if (best >= 0) {
      dragIdx = best;
      canvas.setPointerCapture(e.pointerId);
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (dragIdx < 0) return;
    const { px, py } = canvasPoint(e);
    corners[dragIdx] = { x: clamp(px / view.cw), y: clamp(py / view.ch) };
    draw();
  });
  const endDrag = () => (dragIdx = -1);
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  function close() {
    document.removeEventListener("keydown", onKey);
    overlay.remove();
  }
  function onKey(e) {
    if (e.key === "Escape") close();
  }
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("pointerdown", (e) => {
    if (e.target === overlay) close();
  });

  overlay.querySelector('[data-act="rotate"]').addEventListener("click", () => {
    rotation = (rotation + 90) % 360;
    corners = rotateCW(corners);
    rerender();
  });
  overlay.querySelector('[data-act="auto"]').addEventListener("click", () => {
    let cs = (info.suggestion || DEFAULT_QUAD).map(([x, y]) => ({ x, y }));
    for (let r = rotation; r > 0; r -= 90) cs = rotateCW(cs);
    corners = cs;
    draw();
  });
  overlay.querySelector('[data-act="cancel"]').addEventListener("click", close);
  overlay.querySelector('[data-act="apply"]').addEventListener("click", async () => {
    status.textContent = "Applying…";
    try {
      await postJSON(`/api/photos/${id}/warp`, {
        rotation,
        points: corners.map((c) => [c.x, c.y]),
      });
      close();
      await onDone();
    } catch (e) {
      status.textContent = e.message;
    }
  });
}

// --------------------------- People ---------------------------
async function viewPeople() {
  const view = el(`
    <div>
      <h1>People</h1>
      <div class="people-list" id="peopleList"></div>
    </div>
  `);
  setView(view);
  const list = view.querySelector("#peopleList");
  try {
    const { persons } = await getJSON(`/api/persons`);
    if (!persons.length) {
      list.appendChild(el(`<div class="empty">No people tagged yet.</div>`));
      return;
    }
    for (const p of persons) {
      const row = el(
        `<div class="person-row"><span>${esc(p.name)}</span><span class="muted">${p.photo_count} photo(s)</span></div>`
      );
      row.addEventListener("click", () => (location.hash = `#/person/${p.id}`));
      list.appendChild(row);
    }
  } catch (e) {
    list.appendChild(el(`<div class="empty">${esc(e.message)}</div>`));
  }
}

async function viewPerson(id) {
  const view = el(`
    <div>
      <a class="back" href="#/people">← Back to people</a>
      <h1 id="pname">Person</h1>
      <div id="gridWrap"></div>
    </div>
  `);
  setView(view);
  const wrap = view.querySelector("#gridWrap");
  try {
    const data = await getJSON(`/api/persons/${id}/photos`);
    view.querySelector("#pname").textContent = data.person.name;
    if (!data.photos.length) {
      wrap.appendChild(el(`<div class="empty">No photos.</div>`));
      return;
    }
    const grid = el(`<div class="grid"></div>`);
    for (const p of data.photos) {
      const card = el(
        `<div class="card"><img loading="lazy" src="/media/${esc(p.display_filename || p.filename)}" alt=""/></div>`
      );
      card.addEventListener("click", () => (location.hash = `#/photo/${p.id}`));
      grid.appendChild(card);
    }
    wrap.appendChild(grid);
  } catch (e) {
    wrap.appendChild(el(`<div class="empty">${esc(e.message)}</div>`));
  }
}
