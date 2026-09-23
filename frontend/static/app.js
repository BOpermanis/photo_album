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
        `<div class="card">${badge}<img loading="lazy" src="/media/${esc(p.filename)}" alt=""/></div>`
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
        <div class="stage" id="stage"></div>
        <div class="side" id="side"></div>
      </div>
    </div>
  `);
  setView(view);

  const stage = view.querySelector("#stage");
  const side = view.querySelector("#side");

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

  const img = el(`<img src="/media/${esc(photo.filename)}" alt=""/>`);
  stage.appendChild(img);

  const W = photo.width || 1;
  const H = photo.height || 1;

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
           style="left:${pct(f.x, W)};top:${pct(f.y, H)};width:${pct(f.w, W)};height:${pct(f.h, H)}">
           <span class="num">${i + 1}</span>
           ${named ? `<span class="label">${esc(f.person_name)}</span>` : ""}
         </div>`
      );
      stage.appendChild(box);

      const item = el(`
        <div class="face-item ${named ? "named" : ""}" data-fid="${f.id}">
          <div><span class="num-pill">${i + 1}</span>
            <span class="muted"> confidence ${(f.confidence * 100).toFixed(0)}%</span></div>
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
    render();
  }

  if (img.complete) render();
  else img.addEventListener("load", render);

  // Poll while detection is pending.
  if (!photo.processed) {
    const timer = setInterval(async () => {
      if (!document.body.contains(view)) return clearInterval(timer);
      await reload();
      if (photo.processed) clearInterval(timer);
    }, 2500);
  }
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
        `<div class="card"><img loading="lazy" src="/media/${esc(p.filename)}" alt=""/></div>`
      );
      card.addEventListener("click", () => (location.hash = `#/photo/${p.id}`));
      grid.appendChild(card);
    }
    wrap.appendChild(grid);
  } catch (e) {
    wrap.appendChild(el(`<div class="empty">${esc(e.message)}</div>`));
  }
}
