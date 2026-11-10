// Tests de bout en bout : Playwright pilote la VRAIE application Electron (moteur Python compris)
// sur des archives fictives, à 1366×768, avec une imprimante simulée (fichiers PDF).
//
//   node e2e/run.mjs <dossier de travail préparé par setup_data.py> [dossier de captures]
//
// Chaque scénario joue un usage réel du guichet ; on mesure aussi la réactivité de l'interface :
// la boucle d'événements ne doit jamais être bloquée (aucun gel), même pendant les opérations lourdes.
import { _electron as electron } from "playwright-core";
import { mkdirSync, readdirSync, existsSync, rmSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const app = resolve(here, "..");
const work = resolve(process.argv[2] || "/tmp/e2e_work");
const shots = resolve(process.argv[3] || join(here, "out"));
const printed = join(work, "imprimante");
rmSync(printed, { recursive: true, force: true });
mkdirSync(shots, { recursive: true });
const SIZE = (process.env.E2E_SIZE || "1366x768").split("x").map(Number);

const results = [];
let page, electronApp, shotN = 0;
const py = process.platform === "win32" ? "python" : "python3";

async function shot(name) {
  shotN += 1;
  await page.screenshot({ path: join(shots, `${String(shotN).padStart(2, "0")}_${name}.png`) });
}
async function toastText() {
  return (await page.locator('[data-testid="toast"]').allInnerTexts()).join(" | ");
}
async function waitToast(re, timeout = 60000) {
  await page.locator('[data-testid="toast"]', { hasText: re }).first().waitFor({ timeout });
  return toastText();
}
const printedCount = () => (existsSync(printed) ? readdirSync(printed).length : 0);
async function waitPrinted(n, timeout = 60000) {
  const t = Date.now();
  while (printedCount() < n) { if (Date.now() - t > timeout) throw new Error(`imprimante : ${printedCount()} fichier(s) au lieu de ${n}`); await page.waitForTimeout(200); }
}

async function scenario(name, fn) {
  const t0 = Date.now();
  try {
    await fn();
    results.push({ name, ok: true, ms: Date.now() - t0 });
    console.log(`  ✓ ${name} (${Date.now() - t0} ms)`);
  } catch (err) {
    results.push({ name, ok: false, ms: Date.now() - t0, err: String(err && err.stack || err).slice(0, 1500) });
    console.log(`  ✗ ${name}\n    ${String(err && err.message || err).slice(0, 600)}`);
    await shot(`ECHEC_${name.replace(/\W+/g, "_").slice(0, 40)}`).catch(() => undefined);
    await page.keyboard.press("Escape").catch(() => undefined);
    await page.keyboard.press("Escape").catch(() => undefined);
  }
}

// Sonde de réactivité : retard maximal d'un minuteur de 50 ms dans l'interface (gel = gros retard).
async function startProbe() {
  await page.evaluate(() => {
    window.__lag = 0;
    let last = performance.now();
    window.__probe = setInterval(() => { const now = performance.now(); window.__lag = Math.max(window.__lag, now - last - 50); last = now; }, 50);
  });
}
async function stopProbe() {
  return page.evaluate(() => { clearInterval(window.__probe); return Math.round(window.__lag); });
}

// liste de résultats (2.1) : relevés sur deux lignes, groupés par étudiant
const ROWS = '[data-testid="results"] [data-testid="result-item"]';
const SEL = `${ROWS}.sel`;

/** Mesures de l'aperçu (zone, échelle, part de la page visible) pour le rapport. */
async function measurePreview() {
  return page.evaluate(() => {
    const v = document.querySelector('[data-page="guichet"] [data-testid="viewer"]');
    const st = document.querySelector('[data-page="guichet"] [data-testid="stage"]');
    const c = v.querySelector("canvas");
    const r = v.getBoundingClientRect(), s = st.getBoundingClientRect();
    const ch = c ? c.clientHeight : 0, cw = c ? c.clientWidth : 0;
    return {
      viewer: [Math.round(r.width), Math.round(r.height)], stage: [Math.round(s.width), Math.round(s.height)], win: [innerWidth, innerHeight],
      fit: v.dataset.fit, scale: Number(v.dataset.scale), canvas: [cw, ch],
      visible: ch ? Math.min(1, (v.clientHeight - 12) / ch) : 0, scrollTop: v.scrollTop,
      surface: (r.width * r.height) / (innerWidth * innerHeight),
    };
  });
}
const fmt = (m) => `zone d'aperçu ${m.viewer[0]}×${m.viewer[1]} px (avec le dock ${m.stage[0]}×${m.stage[1]}, ${Math.round(m.surface * 100)} % de la fenêtre ${m.win[0]}×${m.win[1]}) · mode ${m.fit} · échelle ${Math.round(m.scale * 100)} % · ${Math.round(m.visible * 100)} % de la page visible`;
async function waitPage() {
  await page.locator('[data-page="guichet"] [data-testid="viewer"][data-ready="1"] canvas').first().waitFor({ timeout: 60000 });
  await page.waitForTimeout(400);
}

async function search(q) {
  const box = page.getByTestId("search");
  await box.click();
  await box.fill("");
  await box.pressSequentially(q, { delay: 25 });
  await page.waitForTimeout(400);
}

(async () => {
  const exe = process.env.E2E_EXE;          // application installée/compilée (CI Windows) : moteur embarqué
  electronApp = await electron.launch({
    ...(exe ? { executablePath: exe, args: [] } : { args: [app, "--no-sandbox"] }),
    cwd: app,
    env: {
      ...process.env,
      FMSP_DATA_DIR: join(work, "data"),
      SCOLARIX_E2E: "1",
      ...(exe ? {} : { SCOLARIX_ENGINE_CMD: process.env.E2E_ENGINE_CMD || JSON.stringify([py, "-m", "engine.server"]), SCOLARIX_ENGINE_CWD: resolve(app, "..") }),
      SCOLARIX_PRINT_TO_PDF: printed,
    },
  });
  page = await electronApp.firstWindow();
  await electronApp.evaluate(({ BrowserWindow }, [w, h]) => BrowserWindow.getAllWindows()[0].setContentSize(w, h), SIZE);
  page.on("pageerror", (e) => console.log("    [erreur interface]", e.message));
  await page.getByTestId("search").waitFor();
  await page.locator(".banner").waitFor({ state: "detached", timeout: 60000 }).catch(() => undefined);

  await scenario("Accueil : premier écran", async () => {
    await page.waitForTimeout(800);
    await shot("accueil");
  });

  await scenario("Matricule à l'oral « 18 m 042 », relevé N4, Ctrl+P", async () => {
    await search("18 m 042");
    await page.locator(ROWS).first().waitFor();
    for (let i = 0; i < 15; i++) {
      const line = await page.locator(SEL).innerText();
      if (/N4\b/.test(line)) break;
      await page.keyboard.press("ArrowDown");
      await page.waitForTimeout(120);
    }
    await waitPage();                                     // la page du relevé N4, pas celle du précédent
    await shot("niveau4");
    const before = printedCount();
    await page.keyboard.press("Control+p");
    await waitToast(/Envoyé à l'imprimante/);
    await waitPrinted(before + 1);
    await shot("imprime");
  });

  await scenario("Export PDF d'un relevé (Ctrl+E)", async () => {
    for (let i = 0; i < 10; i++) {          // relevé N1 : un PDF (pas besoin de Word)
      const line = await page.locator(SEL).innerText();
      if (/N1\b/.test(line)) break;
      await page.getByTestId("search").press("ArrowUp");
      await page.waitForTimeout(120);
    }
    await page.waitForTimeout(300);
    await page.keyboard.press("Control+e");
    const t = await waitToast(/Exporté \(réf\./);
    if (!/Ouvrir le dossier/.test(t)) throw new Error("bouton « Ouvrir le dossier » absent");
  });

  if (process.env.E2E_NO_WORD) await scenario("Poste sans Word : message clair, délivrance bloquée", async () => {
    await search("2021");
    await page.locator(ROWS).first().waitFor();
    const rows = page.locator(ROWS);
    const n = await rows.count();
    for (let i = 0; i < n; i++) {
      await rows.nth(i).click();
      const msg = page.locator('[data-testid="viewer"] .msg', { hasText: "Microsoft Word n'est pas installé" });
      if (await msg.count() || await msg.waitFor({ timeout: 4000 }).then(() => true, () => false)) {
        await shot("sans_word");
        const before = printedCount();
        await page.keyboard.press("Control+p");
        await waitToast(/indisponible|impossible/i, 10000);
        if (printedCount() !== before) throw new Error("un relevé Word sans aperçu contrôlé a été imprimé");
        return;
      }
    }
    throw new Error("aucun relevé Word avec le message « Microsoft Word n'est pas installé »");
  });

  if (!process.env.E2E_NO_WORD) await scenario("Relevé Word : Ctrl+P immédiat, l'impression part toute seule", async () => {
    await search("2021");
    await page.locator(ROWS).first().waitFor();
    const rows = page.locator(ROWS);
    const n = await rows.count();
    // comme un fichier jamais consulté : on vide le cache des aperçus Word
    for (const f of readdirSync(join(work, "cache"))) rmSync(join(work, "cache", f), { recursive: true, force: true });
    await search("2020");
    await search("2021");
    await page.locator(ROWS).first().waitFor();
    const before = printedCount();
    // un relevé Word (aperçu produit à la demande) : on prend le premier qui affiche « Préparation »
    let found = false;
    for (let i = 0; i < n && !found; i++) {
      await rows.nth(i).click();
      await page.waitForTimeout(200);
      found = await page.locator('[data-testid="viewer"] .msg', { hasText: "Préparation de l'aperçu Word" }).count() > 0;
    }
    if (!found) throw new Error("aucun relevé Word en préparation trouvé : le scénario ne teste rien");
    await page.keyboard.press("Control+p");
    await waitToast(/automatiquement dès qu'il sera prêt/, 5000);
    await shot("word_programme");
    await waitPrinted(before + 1, 120000);
    await shot("word_parti_tout_seul");
  });

  await scenario("↓ maintenu sur tout le dossier : aucun gel, l'aperçu suit", async () => {
    await search("18M042");
    await page.locator(ROWS).first().waitFor();
    await startProbe();
    for (let i = 0; i < 6; i++) await page.getByTestId("search").press("ArrowDown", { delay: 30 });
    await page.waitForTimeout(1500);
    const lag = await stopProbe();
    const sel = await page.locator(SEL).innerText();
    const recap = await page.getByTestId("recap").innerText();
    const per = sel.match(/N\d\b/);
    if (per && !recap.includes(`Niveau ${per[0].slice(1)}`)) throw new Error(`aperçu en retard : ligne « ${sel.split("\n").join(" ")} », fiche « ${recap.split("\n")[1]} »`);
    console.log(`    retard maximal pendant le défilement : ${lag} ms`);
    if (lag > 300) throw new Error(`interface figée ${lag} ms`);
  });

  await scenario("Aperçu d'abord : page lisible, ajustements Auto / Page / 1:1, points de contrôle", async () => {
    await search("18M042");
    await page.locator(ROWS).first().waitFor();
    await waitPage();
    const m = await measurePreview();
    console.log(`    ${SIZE.join("×")} : ${fmt(m)}`);
    if (m.fit !== "auto" || m.scale < 0.68) throw new Error(`mode Auto illisible : ${JSON.stringify(m)}`);
    if (m.canvas[0] > m.viewer[0]) throw new Error(`page plus large que la zone : ${JSON.stringify(m)}`);
    if (m.viewer[1] < 0.7 * m.win[1]) throw new Error(`zone d'aperçu trop basse : ${m.viewer[1]} px`);
    const strip = await page.getByTestId("recap").boundingBox();
    if (strip.height > 92) throw new Error(`fiche sur plus de deux lignes : ${strip.height} px`);
    if (m.visible < 0.999) {                             // zoom de lecture : mini-carte, Alt+3 descend à la décision
      await page.getByTestId("minimap").waitFor({ timeout: 5000 });
      await page.keyboard.press("Alt+3");
      await page.locator('.zone-hl[data-zone="decision"]').waitFor({ timeout: 3000 });
      await page.waitForTimeout(700);
      const top = (await measurePreview()).scrollTop;
      if (top < 50) throw new Error(`Alt+3 n'a pas fait défiler jusqu'à la décision (scrollTop ${top})`);
      await shot("controle_decision");
      await page.keyboard.press("Alt+1");
      await page.locator('.zone-hl[data-zone="identite"]').waitFor({ timeout: 3000 });
      await page.waitForTimeout(700);
      if ((await measurePreview()).scrollTop >= top) throw new Error("Alt+1 n'est pas remonté à l'identité");
    }
    await page.getByTestId("fit-page").click();
    await page.waitForTimeout(700);
    const p = await measurePreview();
    if (p.fit !== "page" || p.visible < 0.98) throw new Error(`mode Page : ${JSON.stringify(p)}`);
    await shot("apercu_page_entiere");
    await page.getByTestId("zone-notes").click();           // page entière illisible : zoom de lecture puis la zone des notes
    await page.locator('.zone-hl[data-zone="notes"]').waitFor({ timeout: 5000 });
    await page.getByTestId("fit-1:1").click();
    await page.waitForTimeout(500);
    if ((await measurePreview()).scale !== 1) throw new Error("mode 1:1");
    await page.getByTestId("fit-auto").click();
    await page.waitForTimeout(500);
    if ((await measurePreview()).fit !== "auto") throw new Error("retour au mode Auto");
    // la liste suit ↑ ↓ dans l'ordre affiché, l'aperçu suit la liste
    const idx = () => page.locator(ROWS).evaluateAll((els) => els.findIndex((e) => e.classList.contains("sel")));
    const i0 = await idx();
    const key = i0 > 0 ? "ArrowUp" : "ArrowDown";
    await page.keyboard.press(key);
    await page.waitForTimeout(300);
    if ((await idx()) !== i0 + (key === "ArrowUp" ? -1 : 1)) throw new Error(`${key} : relevé ${i0} → ${await idx()}`);
  });

  await scenario("Mode focus : Entrée, ↑ ↓, Échap par couches (fenêtre > focus > recherche)", async () => {
    await search("18M042");
    await page.locator(ROWS).first().waitFor();
    await waitPage();
    await page.waitForTimeout(400);
    await page.getByTestId("search").press("Enter");        // recherche à jour : Entrée = plein écran
    const bar = page.getByTestId("focus-bar");
    await bar.waitFor({ timeout: 5000 });
    const st = await page.getByTestId("stage").boundingBox();
    if (Math.round(st.width) !== SIZE[0] || Math.round(st.height) !== SIZE[1]) throw new Error(`plein écran : ${st.width}×${st.height}`);
    await waitPage();
    const m = await measurePreview();
    console.log(`    focus ${SIZE.join("×")} : ${fmt(m)}`);
    await shot("focus");
    const pos = await page.getByTestId("focus-pos").innerText();
    await page.keyboard.press(pos.startsWith("1/") ? "ArrowDown" : "ArrowUp");
    await page.waitForTimeout(400);
    const pos2 = await page.getByTestId("focus-pos").innerText();
    if (pos === pos2) throw new Error(`↓ en mode focus : ${pos} → ${pos2}`);
    // une fenêtre modale passe avant : Échap la ferme, le mode focus reste
    await page.keyboard.press("Control+n");
    await page.getByRole("dialog", { name: "Nouvelle demande" }).waitFor();
    await page.keyboard.press("Escape");
    await page.getByRole("dialog", { name: "Nouvelle demande" }).waitFor({ state: "detached" });
    if (!(await bar.isVisible())) throw new Error("Échap sur la fenêtre a aussi quitté le mode focus");
    await page.keyboard.press("Escape");                   // sortie du mode focus, recherche intacte
    await bar.waitFor({ state: "detached" });
    if ((await page.getByTestId("search").inputValue()) !== "18M042") throw new Error("Échap en mode focus a effacé la recherche");
    await page.keyboard.press("F11");                      // F11 : bascule
    await bar.waitFor();
    await page.locator('[data-page="guichet"] [data-testid="viewer"]').dblclick();   // double-clic : sortie
    await bar.waitFor({ state: "detached" });
    await page.locator(SEL).dblclick();                     // double-clic sur un relevé : plein écran
    await bar.waitFor();
    await page.getByTestId("focus-exit").click();
    await bar.waitFor({ state: "detached" });
    await page.keyboard.press("Escape");                   // puis Échap = nouvelle recherche
    await page.waitForTimeout(200);
    if (await page.getByTestId("search").inputValue()) throw new Error("Échap hors mode focus n'efface pas la recherche");
  });

  await scenario("Grand écran 1920×1080 : page entière, chiffres du jour, mode focus", async () => {
    const resize = (w, h) => electronApp.evaluate(({ BrowserWindow }, [w, h]) => BrowserWindow.getAllWindows()[0].setContentSize(w, h), [w, h]);
    try {
      await resize(1920, 1080);
      await page.waitForTimeout(600);
      await search("18M042");
      await page.locator(ROWS).first().waitFor();
      await waitPage();
      await page.waitForTimeout(600);
      const m = await measurePreview();
      console.log(`    1920×1080 : ${fmt(m)}`);
      if (m.visible < 0.999 || m.scale < 0.68) throw new Error(`page entière attendue à 1920×1080 : ${JSON.stringify(m)}`);
      if (await page.getByTestId("minimap").count()) throw new Error("mini-carte affichée alors que la page est entière");
      await shot("ecran_1920");
      await page.getByTestId("stats-btn").click();
      const pop = await page.getByTestId("stats-pop").innerText();
      if (!/Imprimés aujourd'hui/.test(pop) || !/Anomalies/.test(pop)) throw new Error(`chiffres du jour : ${pop}`);
      await shot("chiffres_du_jour_1920");
      await page.keyboard.press("Escape");
      await page.getByTestId("search").press("Enter");
      await page.getByTestId("focus-bar").waitFor();
      await waitPage();
      await page.waitForTimeout(500);
      console.log(`    focus 1920×1080 : ${fmt(await measurePreview())}`);
      await shot("focus_1920");
      await page.keyboard.press("Escape");
      await page.getByTestId("focus-bar").waitFor({ state: "detached" });
    } finally {
      await resize(SIZE[0], SIZE[1]);
      await page.waitForTimeout(600);
    }
  });

  await scenario("Nom avec fautes « tchoa alin » (tolérance)", async () => {
    await search("tchoa alin");
    const tol = page.getByTestId("tolerance");
    if (!(await tol.locator("input").isChecked())) await tol.click();
    await page.locator(ROWS).first().waitFor({ timeout: 10000 });
    const first = await page.locator('[data-testid="result-group"]').first().innerText();
    if (!/TCHOUA/i.test(first)) throw new Error(`premier étudiant : ${first}`);
    await page.getByTestId("view-groupe").waitFor();
    await shot("groupee");
    // liste à plat : le nom revient sur chaque relevé, ↑ ↓ suivent l'ordre de pertinence
    await page.getByTestId("view-releve").click();
    const flat = await page.locator(ROWS).first().innerText();
    if (!/TCHOUA/i.test(flat)) throw new Error(`liste à plat, premier relevé : ${flat}`);
    await page.getByTestId("view-groupe").click();
    await tol.click();                                   // recherche exacte (comme l'ancien bouton « Exacte »)
    if (await tol.locator("input").isChecked()) throw new Error("l'interrupteur Tolérance ne se désactive pas");
  });

  await scenario("Demande multi-années : Ctrl+D ×3, imprimer tout, remettre", async () => {
    await search("18M042");
    await page.locator(ROWS).first().waitFor();
    // première ajout : la demande est créée (demandeur)
    await page.keyboard.press("Control+d");
    await page.getByRole("dialog", { name: "Nouvelle demande" }).waitFor();
    await page.keyboard.press("Enter");
    await waitToast(/Ajouté à la demande/);
    for (let k = 0; k < 2; k++) {
      await page.getByTestId("search").press("ArrowDown");
      await page.getByTestId("search").press("ArrowDown");
      await page.waitForTimeout(250);
      await page.keyboard.press("Control+d");
      await page.waitForTimeout(400);
    }
    const label = await page.getByTestId("demande-label").innerText();
    if (!/3 relevés/.test(label)) throw new Error(`pastille de demande : ${label}`);
    await page.getByTestId("demande-chip").click();
    await page.getByTestId("demande-menu").waitFor();
    await shot("menu_demande");
    await page.getByTestId("demande-open").click();
    await page.getByRole("dialog").first().waitFor();
    await shot("demande");
    const before = printedCount();
    await page.getByTestId("demande-print").click();
    await waitToast(/Envoyé à l'imprimante/, 120000);
    await waitPrinted(before + 2, 60000);          // bordereau + dossier
    await shot("demande_imprimee");
    await page.getByTestId("demande-done").click();
    await page.getByRole("dialog", { name: "Remise" }).waitFor();
    await page.keyboard.press("Enter");
    await waitToast(/remise et close/);
    await page.keyboard.press("Escape");
    await page.waitForTimeout(400);
  });

  await scenario("Deux personnes : Ctrl+D sur un autre étudiant propose une nouvelle demande", async () => {
    await search("kouame");
    await page.locator(ROWS).first().waitFor();
    await page.keyboard.press("Control+d");
    await page.getByRole("dialog", { name: "Nouvelle demande" }).waitFor();
    await page.locator('[role="dialog"] input').fill("Sa sœur");
    await page.keyboard.press("Enter");
    await waitToast(/Ajouté à la demande/);
    await search("18M042");
    await page.locator(ROWS).first().waitFor();
    await page.keyboard.press("Control+d");
    const dlg = page.getByRole("dialog", { name: "Autre étudiant" });
    await dlg.waitFor();
    await shot("autre_etudiant");
    await dlg.getByRole("button", { name: /Nouvelle demande pour/ }).click();
    await page.getByRole("dialog", { name: "Nouvelle demande" }).waitFor();
    await page.keyboard.press("Enter");
    await waitToast(/Ajouté à la demande/);
    let opts = [];
    for (let i = 0; i < 25; i++) {
      await page.getByTestId("demande-chip").click();
      opts = await page.getByTestId("demande-picker").getByTestId("demande-option").allInnerTexts();
      if (opts.filter((o) => /KOUAME|TCHOUA/i.test(o)).length >= 2) break;
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);
    }
    if (opts.filter((o) => /KOUAME|TCHOUA/i.test(o)).length < 2) throw new Error(`demandes ouvertes : ${opts.join(" / ")}`);
    const kouame = opts.find((o) => /KOUAME/i.test(o));
    if (!/\(1\)/.test(kouame)) throw new Error(`la demande de KOUAME a reçu le relevé d'un autre : ${kouame}`);
    await page.keyboard.press("Escape");                 // Échap ferme le menu seulement
    await page.getByTestId("demande-menu").waitFor({ state: "detached" });
    if (!(await page.getByTestId("search").inputValue())) throw new Error("Échap sur le menu a aussi effacé la recherche");
  });

  await scenario("Cursus : fusion et export, interface jamais figée", async () => {
    await search("18M042");
    await page.locator(ROWS).first().waitFor();
    await page.keyboard.press("Control+Shift+E");
    await page.getByRole("dialog", { name: "Cursus complet" }).waitFor();
    await page.waitForTimeout(400);
    await shot("cursus");
    await startProbe();
    await page.getByTestId("cursus-export").click();
    const t = await waitToast(/Exporté \(réf\.|Échec/, 180000);
    const lag = await stopProbe();
    if (!/Exporté/.test(t)) throw new Error(t);
    console.log(`    retard maximal de l'interface pendant la fusion : ${lag} ms`);
    if (lag > 400) throw new Error(`interface figée ${lag} ms pendant la fusion`);
    await page.keyboard.press("Escape");
  });

  await scenario("Recherche sans résultat : pistes affichées", async () => {
    await search("zzqxw");
    await page.getByTestId("empty").waitFor();
    await shot("aucun_resultat");
  });

  await scenario("Parité 1.x : matricule normalisé affiché, fiche détaillée, Échap depuis un autre écran", async () => {
    await search("18 m 042");
    await page.getByText("Recherché : 18M042").waitFor({ timeout: 10000 });
    await page.locator(ROWS).first().waitFor();
    await page.getByTestId("recap-details").waitFor({ timeout: 10000 });
    const src = await page.getByTestId("recap-reveal").getAttribute("title");     // chemin source en infobulle
    if (!/archives/.test(src || "")) throw new Error(`chemin source absent : ${src}`);
    await page.getByTestId("recap-details").click();                               // moyenne, décision, naissance, source…
    const pop = await page.getByTestId("recap-pop").innerText();
    if (!/Moyenne/.test(pop) || !/archives/.test(pop)) throw new Error(`détails de la fiche : ${pop.slice(0, 200)}`);
    await shot("fiche_details");
    await page.keyboard.press("Escape");
    await page.getByTestId("recap-pop").waitFor({ state: "detached" });
    await page.keyboard.press("Control+4");
    await page.getByRole("heading", { name: "Extraction et journal" }).waitFor();
    await page.locator("body").click({ position: { x: 700, y: 20 } });
    await page.keyboard.press("Escape");
    await page.locator('[data-page="guichet"]').waitFor({ state: "visible" });
    await page.waitForFunction(() => document.activeElement?.getAttribute("data-testid") === "search", null, { timeout: 5000 });
  });

  await scenario("Demande : « Ajouter tout le cursus validé »", async () => {
    await search("18M042");
    await page.locator(ROWS).first().waitFor();
    await page.keyboard.press("Control+n");
    await page.getByRole("dialog", { name: "Nouvelle demande" }).waitFor();
    await page.keyboard.press("Enter");
    await page.waitForTimeout(400);
    await page.keyboard.press("Control+d");
    await waitToast(/Ajouté à la demande/);
    await page.getByTestId("demande-chip").click();
    await page.getByTestId("demande-open").click();
    await page.getByTestId("demande-cursus").click();
    await waitToast(/ajoutés? \(cursus/);
    const n = await page.locator('[role="dialog"] table.grid tbody tr').count();
    if (n < 3) throw new Error(`cursus validé : ${n} relevé(s) dans la demande`);
    await shot("demande_cursus");
    await page.keyboard.press("Escape");
    await page.waitForTimeout(300);
  });

  await scenario("Écran Demandes (Ctrl+2)", async () => {
    await page.keyboard.press("Control+2");
    await page.getByRole("heading", { name: "Demandes de relevés" }).waitFor();
    await page.waitForTimeout(400);
    await shot("demandes");
    await page.locator('[data-page="demandes"]').getByText("Toutes", { exact: true }).click();
    await page.getByTestId("demandes-filter").fill("kouame");
    await page.waitForTimeout(300);
    const rows = await page.locator('[data-page="demandes"] table.grid tbody tr').allInnerTexts();
    if (!rows.length || rows.some((r) => !/KOUAME/i.test(r))) throw new Error(`filtre : ${rows.join(" / ")}`);
    await page.getByTestId("demandes-filter").fill("");
  });

  // ---- Extraction et journal (2.1) : toutes les lignes, filtres avec effectifs, tri, dock ; journal en vue sœur
  await scenario("Extraction Décanat : toutes les lignes, filtres, tri, export Excel (Ctrl+4)", async () => {
    await page.keyboard.press("Control+4");
    await page.getByRole("heading", { name: "Extraction et journal" }).waitFor();
    await page.getByTestId("view-extraction").click();
    const count = async () => Number((await page.getByTestId("xr-count").innerText()).replace(/\D/g, ""));
    await page.locator('[data-testid="xr-table"] tbody tr:not(.vt-pad)').first().waitFor();
    const all = await count();
    if (all < 10) throw new Error(`extraction : ${all} ligne(s)`);
    await shot("extraction");
    // filière : l'effectif affiché est celui obtenu au clic, et une puce apparaît dans la barre
    const nM = Number((await page.getByTestId("fil-M").locator(".n").innerText()).replace(/\D/g, ""));
    await page.getByTestId("fil-M").click();
    await page.waitForFunction((n) => Number(document.querySelector('[data-testid="xr-count"]').textContent.replace(/\D/g, "")) === n, nM, { timeout: 10000 });
    await page.getByTestId("filter-chips").getByText("Médecine").waitFor();
    await page.getByRole("button", { name: "Retirer le filtre Médecine" }).click();
    await page.waitForFunction((n) => Number(document.querySelector('[data-testid="xr-count"]').textContent.replace(/\D/g, "")) === n, all, { timeout: 10000 });
    // tous les relevés (sans le « dernier niveau ») : plus de lignes qu'avant, et plus de plafond à 200
    await page.getByTestId("niveau-choisis").click();
    await page.waitForFunction((n) => Number(document.querySelector('[data-testid="xr-count"]').textContent.replace(/\D/g, "")) > n, all, { timeout: 10000 });
    // tri par moyenne (décroissante au premier clic)
    await page.locator('[data-testid="xr-table"] th', { hasText: "Moyenne" }).click();
    await page.waitForTimeout(300);
    const moys = (await page.locator('[data-testid="xr-table"] tbody tr:not(.vt-pad) td.xr-moy').allInnerTexts()).slice(0, 8).map((x) => parseFloat(x.replace(",", ".")) || -1);
    if (moys.some((m, i) => i && m > moys[i - 1])) throw new Error(`tri par moyenne : ${moys.join(" ")}`);
    // recherche dans le résultat (Ctrl+F reste sur l'écran)
    await page.keyboard.press("Control+f");
    await page.keyboard.type("18M042");
    await page.waitForTimeout(300);
    const rows = await page.locator('[data-testid="xr-table"] tbody tr:not(.vt-pad)').allInnerTexts();
    if (!rows.length || rows.some((r) => !/18M042/.test(r))) throw new Error(`recherche : ${rows.slice(0, 3).join(" / ")}`);
    await page.getByTestId("xr-search").fill("");
    await page.getByTestId("niveau-dernier").click();
    // export Excel (Ctrl+E) des lignes affichées, journalisé, listé dans « Dernières extractions »
    const dest = join(work, "extraction_e2e.xlsx");
    rmSync(dest, { force: true });
    await electronApp.evaluate(({ dialog }, file) => { dialog.showSaveDialog = async () => ({ canceled: false, filePath: file }); }, dest);
    await page.locator('[data-testid="xr-table"]').click({ position: { x: 200, y: 60 } });
    await page.keyboard.press("Control+e");
    await waitToast(/Exporté : extraction_e2e\.xlsx/);
    if (!existsSync(dest)) throw new Error("export Excel non écrit");
    await page.getByTestId("xr-recent").getByText(/lignes · Excel/).waitFor({ timeout: 10000 });
    // ligne choisie : Entrée ouvre l'étudiant au guichet
    await page.locator('[data-testid="xr-table"] tbody tr:not(.vt-pad)').first().click();
    const mat = (await page.locator('[data-testid="xr-table"] tbody tr.sel td').first().innerText()).trim();
    await page.keyboard.press("Enter");
    await page.locator('[data-page="guichet"]').waitFor({ state: "visible" });
    await page.waitForFunction((m) => document.querySelector('[data-page="guichet"] [data-testid="search"]')?.value === m, mat, { timeout: 5000 });
  });

  await scenario("Journal des actions : vue sœur, période, filtres, export CSV", async () => {
    await page.keyboard.press("Control+4");
    await page.getByTestId("view-journal").click();
    await page.locator('[data-testid="journal-table"] tbody tr:not(.vt-pad)').first().waitFor();
    const text = await page.getByTestId("journal-table").innerText();
    if (!/CONSULTATION/.test(text) || !/EXTRACTION/.test(text)) throw new Error("consultations ou extraction absentes du journal");
    const total = Number((await page.getByTestId("journal-count").innerText()).replace(/\D/g, ""));
    if (total < 3) throw new Error(`journal : ${total} ligne(s)`);
    // filtre par type d'action (effectifs à côté)
    await page.getByTestId("journal-actions").locator("label", { hasText: "EXTRACTION" }).locator("input").uncheck();
    // (le guichet a pu journaliser une consultation entre-temps : on vérifie le contenu, pas un total)
    await page.waitForFunction(() => !/EXTRACTION/.test(document.querySelector('[data-testid="journal-table"]').innerText), null, { timeout: 10000 });
    await page.getByTestId("filter-chips").getByText("sans EXTRACTION").waitFor();
    await page.getByRole("button", { name: /Retirer le filtre/ }).nth(1).click();
    await page.getByTestId("journal-period-tout").click();
    await shot("journal");
    const dest = join(work, "journal_e2e.csv");
    rmSync(dest, { force: true });
    await electronApp.evaluate(({ dialog }, file) => { dialog.showSaveDialog = async () => ({ canceled: false, filePath: file }); }, dest);
    await page.getByTestId("journal-export").click();
    await waitToast(/Journal exporté/);
    if (!existsSync(dest)) throw new Error("journal CSV non écrit");
    await page.keyboard.press("Control+1");             // la vue Journal reste choisie au retour sur l'écran
  });

  await scenario("Vérification : original retrouvé tout seul (Ctrl+5)", async () => {
    await page.keyboard.press("Control+5");
    const exportsDir = join(work, "Exports Scolarix");
    const pdfs = [];
    const walk = (d) => { for (const f of readdirSync(d, { withFileTypes: true })) { const p = join(d, f.name); if (f.isDirectory()) walk(p); else if (f.name.startsWith("FMSP_") && f.name.endsWith(".pdf") && !/Cursus/.test(f.name)) pdfs.push(p); } };
    walk(exportsDir);
    if (!pdfs.length) throw new Error("aucun relevé exporté à vérifier");
    await electronApp.evaluate(({ dialog }, file) => { dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [file] }); }, pdfs[0]);
    await page.getByTestId("verify-load").click();
    await page.getByTestId("verdict").waitFor({ timeout: 120000 });
    const v = await page.getByTestId("verdict").innerText();
    const mat = await page.getByTestId("verify-mat").inputValue();
    if (!/conforme/i.test(v) || !mat) throw new Error(`verdict « ${v} », matricule « ${mat} »`);
    await page.locator('[data-page="verification"] [data-testid="viewer"] canvas').first().waitFor({ timeout: 30000 });
    await page.locator('[data-testid="verify-submitted"] canvas').first().waitFor({ timeout: 30000 });
    await shot("verification");
    const before = printedCount();
    await page.getByTestId("verify-print").click();
    await waitToast(/timbre « Vérifié »/, 60000);
    await waitPrinted(before + 1);
  });

  await scenario("Indexation (Ctrl+3) : relance différentielle", async () => {
    await page.keyboard.press("Control+3");
    await page.getByTestId("stats-row").getByText("Imprimés aujourd'hui").waitFor();     // tuiles venues du guichet
    await page.getByTestId("index-start").click();
    await page.getByText("TERMINÉ").first().waitFor({ timeout: 120000 });
    await shot("indexation");
    await page.getByTestId("index-kpis").getByText("Fichiers en échec").waitFor();
    await page.getByTestId("index-integrity").click();
    await waitToast(/Intégrité vérifiée/);
  });

  await scenario("Vider l'index puis tout réindexer (journal et registre conservés)", async () => {
    await page.getByTestId("index-reset").click();
    const dlg = page.getByRole("dialog", { name: "Vider l'index" });
    await dlg.waitFor();
    const text = await dlg.innerText();
    if (!/registre des délivrances \(\d+/.test(text)) throw new Error(`fenêtre : ${text.slice(0, 200)}`);
    await shot("vider_index");
    await dlg.getByRole("button", { name: "Vider et tout réindexer" }).click();
    await waitToast(/Index vidé/);
    await page.getByText("TERMINÉ").first().waitFor({ timeout: 180000 });
    await page.keyboard.press("Control+1");
    await search("18M042");
    await page.locator(ROWS).first().waitFor({ timeout: 30000 });
    await page.keyboard.press("Control+4");
    await page.waitForTimeout(800);
    const j = await page.locator("table.grid").last().innerText();
    if (!/INDEX VIDÉ/.test(j) || !/IMPRESSION|EXPORT/.test(j)) throw new Error("journal incomplet après le vidage");
    await page.keyboard.press("Control+3");
  });

  await scenario("Taille de l'interface : Ctrl+Plus immédiat, Ctrl+0", async () => {
    await page.keyboard.press("Control+1");
    const z0 = await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor());
    await page.keyboard.press("Control+Equal");
    await page.waitForTimeout(400);
    const z1 = await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor());
    await page.keyboard.press("Control+0");
    await page.waitForTimeout(400);
    const z2 = await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getZoomFactor());
    if (!(z1 > z0 && Math.abs(z2 - 1) < 0.01)) throw new Error(`zoom ${z0} → ${z1} → ${z2}`);
  });

  await scenario("Moteur tué en pleine session : relancé, le guichet continue", async () => {
    const pid = await electronApp.evaluate(() => {
      // eslint-disable-next-line no-undef
      return process.pid;
    });
    void pid;
    // on retrouve le PID du moteur Python enfant et on le tue brutalement
    const { execSync } = await import("node:child_process");
    if (process.platform === "win32") {
      execSync(exe ? "taskkill /F /IM scolarix-engine.exe" : "wmic process where \"commandline like '%engine.server%' and name like 'python%'\" call terminate");
    } else {
      const out = execSync("pgrep -f '^python3? -m engine[.]server|^/[^ ]*/scolarix-engine$' || true").toString().trim().split(/\s+/).filter(Boolean);
      if (!out.length) throw new Error("moteur introuvable");
      execSync(`kill -9 ${out.join(" ")}`);
    }
    await waitToast(/moteur a redémarré|ne répondait plus/i, 30000);
    await page.waitForTimeout(3000);
    await search("18M042");
    await page.locator(ROWS).first().waitFor({ timeout: 30000 });
    await shot("apres_relance_moteur");
  });

  // ---- Réglages (2.1) : feuille large, navigation, recherche Ctrl+K, modifications nommées, outils, remplacements
  await scenario("Réglages : ouverture, outils du poste, modifications nommées, Ctrl+S", async () => {
    await page.keyboard.press("Control+1");
    await page.getByTestId("stats-btn").click();                 // chiffres du jour : poste et opérateur
    await page.getByTestId("settings").click();
    const dlg = page.getByRole("dialog", { name: "Réglages" });
    await dlg.waitFor();
    await page.getByTestId("sec-outils").click();
    await page.getByTestId("tool-tesseract").getByText(/DÉTECTÉ|INTROUVABLE/).waitFor({ timeout: 20000 });
    await page.getByTestId("tool-libreoffice").getByText(/DÉTECTÉ|INTROUVABLE/).waitFor();
    await page.getByTestId("tool-db").getByText(/relevés/).waitFor({ timeout: 10000 });
    await shot("reglages_outils");
    await page.getByTestId("sec-diagnostic").click();
    await page.getByTestId("settings-version").waitFor();
    await page.getByTestId("sec-indexation").click();
    await page.getByTestId("exclusion-pattern").click();
    await page.getByRole("dialog", { name: "Motif à exclure" }).locator("input").fill("*BROUILLON*");
    await page.keyboard.press("Enter");
    await page.getByTestId("settings-pending").getByText("exclusions").waitFor();
    await page.getByTestId("exclusions").getByText("*BROUILLON*").click();
    await page.getByTestId("exclusion-remove").click();
    if (await page.getByTestId("exclusions").getByText("*BROUILLON*").count()) throw new Error("motif non retiré");
    if (await page.getByTestId("settings-pending").count()) throw new Error("modification fantôme après retour à l'état d'origine");
    // recherche dans les réglages : Ctrl+K, Entrée mène à la section et au champ
    await page.keyboard.press("Control+k");
    await page.keyboard.type("recto");
    await page.keyboard.press("Enter");
    await page.locator('[data-section="impression"] [data-set="duplex"]').waitFor();
    await page.getByTestId("sec-operateur").click();
    const op = page.getByTestId("set-operateur");
    const before = await op.inputValue();
    await op.fill(`${before} (test)`);
    await page.getByTestId("settings-pending").getByText(/1 modification non enregistrée : opérateur/).waitFor();
    await shot("reglages_modifications");
    await op.fill(before);
    await page.keyboard.press("Control+s");
    await waitToast(/Réglages enregistrés/);
    await dlg.waitFor({ state: "detached" });
  });

  await scenario("Réglages : barre latérale, Ctrl+, et remplacements Word (texte libre)", async () => {
    const dlg = page.getByRole("dialog", { name: "Réglages" });
    await page.getByTestId("nav-reglages").click();
    await dlg.waitFor();
    await page.keyboard.press("Escape");
    await dlg.waitFor({ state: "detached" });
    await page.keyboard.press("Control+,");
    await dlg.waitFor();
    await page.getByTestId("sec-remplacements").click();
    await page.getByTestId("rules-texte").click();
    const result = page.getByTestId("rules-result");
    await result.filter({ hasText: "3 remplacement(s)" }).waitFor({ timeout: 10000 });
    if (!/Ref N° RELEVE DE NOTES/.test(await result.innerText())) throw new Error("essai : « Ref N° » absent");
    await page.getByTestId("rules-after").locator(".hd-ins").first().waitFor();          // ajouté en vert
    await page.getByTestId("rules-preview").locator(".hd-del").first().waitFor();         // retiré barré en rouge
    await shot("reglages_remplacements_texte");
    // une règle ajoutée (Ctrl+N) est essayée aussitôt
    await page.keyboard.press("Control+n");
    await page.getByTestId("rule-chercher").fill("TRANSCRIPT");
    await page.getByTestId("rule-remplacer").fill("RELEVÉ");
    await result.filter({ hasText: "4 remplacement(s)" }).waitFor({ timeout: 10000 });
    await page.getByTestId("settings-pending").getByText(/1 règle\(s\) ajoutée\(s\)/).waitFor();
    // Alt+↑ : la règle remonte (l'ordre fait partie des modifications)
    await page.keyboard.press("Alt+ArrowUp");
    await page.getByTestId("rule-2").getByText(/3\. Nouvelle règle/).waitFor();
    await page.getByTestId("settings-pending").getByText(/règle/).waitFor();
    await page.keyboard.press("Alt+ArrowDown");
    // une expression invalide est signalée et l'enregistrement refusé, la fenêtre reste ouverte
    await page.getByTestId("rule-regex").click();
    await page.getByTestId("rule-chercher").fill("(TRANSCRIPT");
    await result.filter({ hasText: "Règle 4" }).waitFor({ timeout: 10000 });
    await page.getByTestId("settings-save").click();
    await waitToast(/Enregistrement impossible.*Corrections Word/);
    await dlg.waitFor();
    // Échap sur la question ne ferme que la question, pas les Réglages
    await page.getByTestId("rules-restore").click();
    await page.getByRole("dialog", { name: "Rétablir les règles d'origine" }).waitFor();
    await page.keyboard.press("Escape");
    await page.getByRole("dialog", { name: "Rétablir les règles d'origine" }).waitFor({ state: "detached" });
    await dlg.waitFor();
    await page.getByTestId("rules-restore").click();
    await page.getByRole("dialog", { name: "Rétablir les règles d'origine" }).getByRole("button", { name: "Rétablir" }).click();
    await result.filter({ hasText: "3 remplacement(s)" }).waitFor({ timeout: 10000 });
    await page.getByTestId("settings-save").click();
    await waitToast(/Réglages enregistrés/);
    await dlg.waitFor({ state: "detached" });
  });

  await scenario("Remplacements : AVANT / APRÈS rendu par le moteur sur l'en-tête d'un vrai relevé Word", async () => {
    const dlg = page.getByRole("dialog", { name: "Réglages" });
    await page.keyboard.press("Control+,");
    await dlg.waitFor();
    await page.getByTestId("sec-remplacements").click();
    const src = page.getByTestId("rules-source");
    await page.waitForFunction(() => [...document.querySelectorAll('[data-testid="rules-source"] option')].some((o) => /^\d+$/.test(o.value)), null, { timeout: 15000 }).catch(() => undefined);
    const first = await src.locator("option").first().getAttribute("value");
    if (!first || !/^\d+$/.test(first)) throw new Error("aucun relevé Word proposé pour l'essai");
    await src.selectOption(first);
    // règle propre aux relevés fictifs (titre accentué) : « Ref N° » ajouté devant, en 7 pt
    await page.keyboard.press("Control+n");
    await page.getByTestId("rule-description").fill("Ref N° devant le titre (essai)");
    await page.getByTestId("rule-chercher").fill("RELEVÉ DE NOTES");
    await page.getByTestId("rule-remplacer").fill("Ref N° RELEVÉ DE NOTES");
    await page.getByTestId("rule-taille").fill("7");
    const after = page.getByTestId("rules-after-img");
    await after.locator("img").waitFor({ timeout: 60000 });
    await after.locator(".hd-rect.ins").first().waitFor({ timeout: 60000 });     // texte ajouté situé sur le rendu
    await page.getByTestId("rules-preview").getByText(/[1-9]\d* remplacements?/).first().waitFor();
    await page.waitForTimeout(400);
    await shot("reglages_remplacements");
    await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1920, 1080));
    await page.waitForTimeout(900);
    await shot("reglages_remplacements_1920");
    await electronApp.evaluate(({ BrowserWindow }, [w, h]) => BrowserWindow.getAllWindows()[0].setContentSize(w, h), SIZE);
    await page.waitForTimeout(500);
    // Échap avec des modifications : confirmation, puis abandon (rien n'est enregistré)
    await page.keyboard.press("Escape");
    const q = page.getByRole("dialog", { name: "Modifications non enregistrées" });
    await q.waitFor();
    await q.getByRole("button", { name: "Abandonner les modifications" }).click();
    await dlg.waitFor({ state: "detached" });
    await page.keyboard.press("Control+,");
    await dlg.waitFor();
    await page.getByTestId("sec-remplacements").click();
    if (await page.getByText("Ref N° devant le titre (essai)").count()) throw new Error("règle abandonnée pourtant conservée");
    await page.keyboard.press("Escape");
    await dlg.waitFor({ state: "detached" });
  });

  await scenario("Grand écran 1920×1080 : Extraction, Journal, Réglages", async () => {
    await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1920, 1080));
    await page.waitForTimeout(600);
    await page.keyboard.press("Control+4");
    await page.getByTestId("view-extraction").click();
    await page.locator('[data-testid="xr-table"] tbody tr:not(.vt-pad)').first().waitFor();
    await page.waitForTimeout(500);
    const visible = await page.evaluate(() => { const w = document.querySelector('[data-testid="xr-table"]'); return Math.floor((w.clientHeight - 36) / 34); });
    console.log(`    Extraction à 1920×1080 : ${visible} lignes visibles`);
    await shot("extraction_1920");
    await page.getByTestId("view-journal").click();
    await page.locator('[data-testid="journal-table"] tbody tr:not(.vt-pad)').first().waitFor();
    await shot("journal_1920");
    await page.keyboard.press("Control+,");
    await page.getByRole("dialog", { name: "Réglages" }).waitFor();
    await page.getByTestId("sec-outils").click();
    await page.getByTestId("tool-tesseract").getByText(/DÉTECTÉ|INTROUVABLE/).waitFor({ timeout: 20000 });
    await shot("reglages_outils_1920");
    await page.keyboard.press("Escape");
    await electronApp.evaluate(({ BrowserWindow }, [w, h]) => BrowserWindow.getAllWindows()[0].setContentSize(w, h), SIZE);
    await page.waitForTimeout(400);
    await page.keyboard.press("Control+1");
  });

  await electronApp.close();
  const ko = results.filter((r) => !r.ok);
  console.log(`\n${results.length - ko.length}/${results.length} scénarios réussis · imprimante simulée : ${printedCount()} document(s)`);
  if (ko.length) { for (const r of ko) console.log(`\n✗ ${r.name}\n${r.err}`); process.exit(1); }
})().catch(async (err) => {
  console.error(err);
  try { await electronApp?.close(); } catch { /* */ }
  process.exit(2);
});
