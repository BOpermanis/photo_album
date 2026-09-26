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
  [/^\/albums$/, viewAlbums],
  [/^\/jobs$/, viewJobs],
  [/^\/upload$/, viewUpload],
  [/^\/photos$/, viewPhotos],
  [/^\/photo\/(\d+)$/, viewPhotoDetail],
  [/^\/people$/, viewPeople],
  [/^\/person\/(\d+)$/, viewPerson],
];

function router() {
  const hash = location.hash.replace(/^#/, "") || "/albums";
  updateAlbumChip();
  for (const [re, handler] of routes) {
    const m = hash.match(re);
    if (m) {
      handler(...m.slice(1));
      return;
    }
  }
  location.hash = "#/albums";
}

window.addEventListener("hashchange", router);
window.addEventListener("load", router);

function setView(node) {
  app.innerHTML = "";
  app.appendChild(node);
}

// --------------------------- Active album ---------------------------
function getActiveAlbum() {
  const id = localStorage.getItem("activeAlbumId");
  if (!id) return null;
  return { id: Number(id), name: localStorage.getItem("activeAlbumName") || `Album ${id}` };
}

function setActiveAlbum(id, name) {
  localStorage.setItem("activeAlbumId", String(id));
  localStorage.setItem("activeAlbumName", name || "");
  updateAlbumChip();
}

function updateAlbumChip() {
  const chip = document.getElementById("albumChip");
  if (!chip) return;
  const a = getActiveAlbum();
  chip.textContent = a ? `📁 ${a.name}` : "No album";
}

// --------------------------- Upload ---------------------------
function viewUpload() {
  const active = getActiveAlbum();
  const view = el(`
    <div>
      <h1>Upload photos</h1>
      <div class="uploader">
        <p class="muted">Uploading to <strong>${active ? esc(active.name) : "no album"}</strong>.
          ${active ? "" : `Pick one in <a href="#/albums">Albums</a> first.`}<br/>
          Take photos with your camera, then pick them here. Full resolution is preserved.</p>
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
    const album = getActiveAlbum();
    if (album) fd.append("album_id", String(album.id));
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
  const active = getActiveAlbum();
  if (!active) {
    location.hash = "#/albums";
    return;
  }
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const filter = params.get("filter") || "all";

  const view = el(`
    <div>
      <h1>Photos <span class="muted">— ${esc(active.name)}</span></h1>
      <div class="toolbar">
        <a href="#/photos?filter=all" class="chip ${filter === "all" ? "active" : ""}">All</a>
        <a href="#/photos?filter=untagged" class="chip ${filter === "untagged" ? "active" : ""}">Needs tagging</a>
        <span class="toolbar-spacer"></span>
        <a href="#/albums" class="chip">Switch album</a>
        <a href="#/upload" class="chip">Upload</a>
      </div>
      <div id="gridWrap"></div>
    </div>
  `);
  setView(view);

  const wrap = view.querySelector("#gridWrap");
  try {
    const { photos } = await getJSON(
      `/api/photos?filter=${filter}&album_id=${active.id}`
    );
    if (!photos.length) {
      wrap.appendChild(
        el(`<div class="empty">No photos in this album. <a href="#/upload">Upload some</a>.</div>`)
      );
      return;
    }
    const grid = el(`<div class="grid"></div>`);
    for (const p of photos) {
      let badge = "";
      if (p.align_pending) badge = `<span class="badge">aligning…</span>`;
      else if (!p.processed || p.detect_pending) badge = `<span class="badge">detecting…</span>`;
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
const SVGNS = "http://www.w3.org/2000/svg";

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

  // Which image space we're viewing: "aligned" (perspective-corrected) or
  // "original". Auto-follows the aligned copy once it exists unless the user
  // explicitly toggles.
  let space = photo.has_edit ? "aligned" : "original";
  let spaceLocked = false;

  const dims = () =>
    space === "aligned"
      ? [photo.width || 1, photo.height || 1]
      : [photo.original_width || photo.width || 1, photo.original_height || photo.height || 1];
  const srcName = () =>
    space === "aligned"
      ? photo.edited_filename || photo.display_filename || photo.filename
      : photo.filename;

  const img = el(`<img src="/media/${esc(srcName())}" alt=""/>`);
  const overlay = document.createElementNS(SVGNS, "svg");
  overlay.setAttribute("class", "face-overlay");
  overlay.setAttribute("preserveAspectRatio", "xMidYMid meet");
  const labels = el(`<div class="face-labels"></div>`);
  stage.appendChild(img);
  stage.appendChild(overlay);
  stage.appendChild(labels);

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

  function polyOf(f) {
    let poly = space === "aligned" ? f.poly_aligned : f.poly_original;
    if (!poly || !poly.length) {
      poly =
        f.poly_original && f.poly_original.length
          ? f.poly_original
          : [
              [f.x, f.y],
              [f.x + f.w, f.y],
              [f.x + f.w, f.y + f.h],
              [f.x, f.y + f.h],
            ];
    }
    return poly;
  }

  function render() {
    const [W, H] = dims();
    overlay.setAttribute("viewBox", `0 0 ${W} ${H}`);
    overlay.innerHTML = "";
    labels.innerHTML = "";
    side.querySelectorAll(".face-item, .side-head, .empty, .stage-note").forEach((n) => n.remove());

    refreshTools();

    if (photo.align_pending) {
      side.appendChild(
        el(`<div class="empty">Aligning image…<br/><span class="muted">boxes update when it's ready</span></div>`)
      );
    }
    if (!photo.processed || photo.detect_pending) {
      side.appendChild(
        el(`<div class="empty">Detecting faces…<br/><span class="muted">this runs in the background</span></div>`)
      );
      return;
    }
    if (!photo.faces.length) {
      side.appendChild(el(`<div class="empty">No faces detected.</div>`));
      return;
    }

    side.appendChild(el(`<div class="side-head"><strong>${photo.faces.length} face(s)</strong></div>`));

    photo.faces.forEach((f, i) => {
      const named = f.person_id != null;
      const poly = polyOf(f);
      const pointsAttr = poly.map((p) => `${p[0]},${p[1]}`).join(" ");
      const polyEl = document.createElementNS(SVGNS, "polygon");
      polyEl.setAttribute("points", pointsAttr);
      polyEl.setAttribute("class", `facepoly ${named ? "named" : ""}`);
      overlay.appendChild(polyEl);

      const [W2, H2] = dims();
      const cx = (poly.reduce((s, p) => s + p[0], 0) / poly.length / W2) * 100;
      const cy = (poly.reduce((s, p) => s + p[1], 0) / poly.length / H2) * 100;
      const label = el(
        `<div class="facelabel ${named ? "named" : ""}" style="left:${cx}%;top:${cy}%">${i + 1}${named ? ": " + esc(f.person_name) : ""}</div>`
      );
      labels.appendChild(label);

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
        polyEl.classList.toggle("active", on);
      };
      polyEl.addEventListener("click", () => {
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
    applyPhoto(photo);
  }

  // Apply a fresh photo payload (from a fetch or an SSE push) and re-render.
  function applyPhoto(next) {
    photo = next;
    if (!spaceLocked && photo.has_edit) space = "aligned";
    if (space === "aligned" && !photo.has_edit) space = "original";
    const src = `/media/${esc(srcName())}`;
    if (!img.src.endsWith(src)) img.src = src;
    render();
    ensureStream();
  }

  // --- Static stage tools (built once; visibility refreshed on render) ---
  const drawBtn = el(`<button class="secondary draw-toggle">+ Add face box</button>`);
  const drawHint = el(`<span class="muted draw-hint" hidden>Drag on the photo to mark a face.</span>`);
  const adjustBtn = el(`<button class="secondary">✂ Adjust / Crop</button>`);
  const rerunBtn = el(`<button class="secondary">↻ Rerun face detection</button>`);
  const resetBtn = el(`<button class="secondary" hidden>↩ Revert to original</button>`);
  const spaceToggle = el(`
    <span class="space-toggle" hidden>
      <button data-sp="aligned">Aligned</button>
      <button data-sp="original">Original</button>
    </span>
  `);
  const statusNote = el(`<span class="muted stage-status"></span>`);
  stageTools.appendChild(drawBtn);
  stageTools.appendChild(adjustBtn);
  stageTools.appendChild(rerunBtn);
  stageTools.appendChild(resetBtn);
  stageTools.appendChild(spaceToggle);
  stageTools.appendChild(drawHint);
  stageTools.appendChild(statusNote);

  adjustBtn.addEventListener("click", () => openAdjustEditor(id, reload));
  rerunBtn.addEventListener("click", async () => {
    try {
      await postJSON(`/api/photos/${id}/rerun-detection`, {});
      await reload();
    } catch (e) {
      alert(e.message);
    }
  });
  resetBtn.addEventListener("click", async () => {
    if (!confirm("Discard the crop and go back to the original photo?")) return;
    try {
      await postJSON(`/api/photos/${id}/warp/reset`, {});
      spaceLocked = false;
      space = "original";
      await reload();
    } catch (e) {
      alert(e.message);
    }
  });
  spaceToggle.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      space = b.dataset.sp;
      spaceLocked = true;
      img.src = `/media/${esc(srcName())}`;
      render();
      syncSideHeight();
    });
  });

  function refreshTools() {
    resetBtn.hidden = !photo.has_edit;
    spaceToggle.hidden = !photo.has_edit;
    spaceToggle.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.sp === space);
    });
    rerunBtn.disabled = photo.detect_pending || photo.align_pending;
    let note = "";
    if (photo.align_pending) note = "Aligning…";
    else if (photo.detect_pending) note = "Detecting…";
    statusNote.textContent = note;
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
    const [W, H] = dims();
    try {
      await postJSON(`/api/photos/${id}/faces`, {
        x: left * W,
        y: top * H,
        w: w * W,
        h: h * H,
        space,
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

  // Reactive updates: subscribe to a server-sent event stream that pushes the
  // photo detail the instant background work (align/detect) changes state.
  let stream = null;
  function closeStream() {
    if (stream) {
      stream.close();
      stream = null;
    }
  }
  function ensureStream() {
    const pending = !photo.processed || photo.detect_pending || photo.align_pending;
    if (!document.body.contains(view)) {
      closeStream();
      return;
    }
    if (!pending) {
      closeStream();
      return;
    }
    if (stream) return;
    stream = new EventSource(`/api/photos/${id}/stream`);
    stream.onmessage = (ev) => {
      if (!document.body.contains(view)) {
        closeStream();
        return;
      }
      try {
        applyPhoto(JSON.parse(ev.data));
      } catch {
        /* ignore malformed frame */
      }
    };
    const stop = () => closeStream();
    stream.addEventListener("done", stop);
    stream.addEventListener("gone", stop);
    // A "timeout" frame closes the long-lived stream; reopen if still pending.
    stream.addEventListener("timeout", () => {
      closeStream();
      ensureStream();
    });
    stream.onerror = () => {
      if (!document.body.contains(view)) closeStream();
    };
  }
  ensureStream();
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
  status.textContent = info.current
    ? "Loaded your previous crop."
    : info.detected
    ? "Corners set from auto-detected edges — drag to fine-tune."
    : "Couldn't auto-detect edges — drag the corners onto the photo.";

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
    status.textContent = info.detected
      ? "Snapped to detected edges. Fine-tune by dragging."
      : "No clear photo edges found — drag the corners manually.";
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

// --------------------------- Albums ---------------------------
async function viewAlbums() {
  const view = el(`
    <div>
      <h1>Albums</h1>
      <div class="toolbar">
        <button id="newAlbum">+ New album</button>
        <span class="muted">Pick an album to work in; uploads and the photo grid are scoped to it.</span>
      </div>
      <div class="album-list" id="albumList"></div>
    </div>
  `);
  setView(view);
  const list = view.querySelector("#albumList");

  view.querySelector("#newAlbum").addEventListener("click", async () => {
    try {
      await postJSON(`/api/albums`, {});
      await load();
    } catch (e) {
      alert(e.message);
    }
  });

  async function load() {
    list.innerHTML = "";
    let albums;
    try {
      ({ albums } = await getJSON(`/api/albums`));
    } catch (e) {
      list.appendChild(el(`<div class="empty">${esc(e.message)}</div>`));
      return;
    }
    const activeId = getActiveAlbum() ? getActiveAlbum().id : null;
    if (!albums.length) {
      list.appendChild(el(`<div class="empty">No albums yet. Create one to start.</div>`));
      return;
    }
    for (const a of albums) {
      const isActive = a.id === activeId;
      const card = el(`
        <div class="album-card ${isActive ? "active" : ""}">
          <div class="album-head">
            <input class="album-name" value="${esc(a.name)}" />
            <button class="secondary save-name">Rename</button>
          </div>
          <div class="album-stats">
            <span><strong>${a.image_count}</strong> images</span>
            <span><strong>${a.face_count}</strong> faces</span>
            <span><strong>${a.identity_count}</strong> identities</span>
          </div>
          <div class="album-actions">
            <button class="work">${isActive ? "✓ Working here" : "Work in this album"}</button>
          </div>
        </div>
      `);
      const nameInput = card.querySelector(".album-name");
      card.querySelector(".save-name").addEventListener("click", async () => {
        try {
          const r = await api(`/api/albums/${a.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: nameInput.value }),
          });
          if (getActiveAlbum() && getActiveAlbum().id === a.id)
            setActiveAlbum(a.id, r.name);
          await load();
        } catch (e) {
          alert(e.message);
        }
      });
      card.querySelector(".work").addEventListener("click", () => {
        setActiveAlbum(a.id, nameInput.value.trim() || a.name);
        location.hash = "#/photos";
      });
      list.appendChild(card);
    }
  }

  await load();
}

// --------------------------- Jobs ---------------------------
async function viewJobs() {
  const view = el(`
    <div>
      <h1>Jobs</h1>
      <p class="muted">Background face detection &amp; alignment, newest first. Failed jobs stay listed with their error.</p>
      <div id="jobsWrap"></div>
    </div>
  `);
  setView(view);
  const wrap = view.querySelector("#jobsWrap");
  let timer = null;

  async function load() {
    let jobs;
    try {
      ({ jobs } = await getJSON(`/api/jobs`));
    } catch (e) {
      wrap.innerHTML = "";
      wrap.appendChild(el(`<div class="empty">${esc(e.message)}</div>`));
      return;
    }
    wrap.innerHTML = "";
    if (!jobs.length) {
      wrap.appendChild(el(`<div class="empty">No jobs yet.</div>`));
      return;
    }
    const listEl = el(`<div class="jobs-list"></div>`);
    let anyActive = false;
    for (const j of jobs) {
      if (j.status === "pending" || j.status === "running") anyActive = true;
      const thumb = j.display_filename
        ? `<img loading="lazy" src="/media/${esc(j.display_filename)}" alt=""/>`
        : "";
      const when = j.updated_at
        ? new Date(j.updated_at * 1000).toLocaleTimeString()
        : "";
      const row = el(`
        <a class="job-row" href="#/photo/${j.photo_id}">
          <div class="job-thumb">${thumb}</div>
          <div class="job-main">
            <div class="job-kind">${esc(j.kind)} <span class="muted">· photo ${j.photo_id}</span></div>
            ${j.error ? `<div class="job-error">${esc(j.error)}</div>` : `<div class="muted job-when">${when}</div>`}
          </div>
          <span class="job-status ${esc(j.status)}">${esc(j.status)}</span>
        </a>
      `);
      listEl.appendChild(row);
    }
    wrap.appendChild(listEl);

    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (anyActive) {
      timer = setTimeout(() => {
        if (document.body.contains(view)) load();
      }, 2000);
    }
  }

  await load();
}
