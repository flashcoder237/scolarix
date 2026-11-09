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
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
    for (let i = 0; i < 15; i++) {
      const line = await page.locator('[data-testid="results"] tbody tr.sel').innerText();
      if (/N4\b/.test(line)) break;
      await page.keyboard.press("ArrowDown");
      await page.waitForTimeout(120);
    }
    await page.locator('[data-testid="viewer"] canvas').first().waitFor({ timeout: 60000 });
    await shot("niveau4");
    const before = printedCount();
    await page.keyboard.press("Control+p");
    await waitToast(/Envoyé à l'imprimante/);
    await waitPrinted(before + 1);
    await shot("imprime");
  });

  await scenario("Export PDF d'un relevé (Ctrl+E)", async () => {
    for (let i = 0; i < 10; i++) {          // relevé N1 : un PDF (pas besoin de Word)
      const line = await page.locator('[data-testid="results"] tbody tr.sel').innerText();
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
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
    const rows = page.locator('[data-testid="results"] tbody tr');
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
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
    const rows = page.locator('[data-testid="results"] tbody tr');
    const n = await rows.count();
    // comme un fichier jamais consulté : on vide le cache des aperçus Word
    for (const f of readdirSync(join(work, "cache"))) rmSync(join(work, "cache", f), { recursive: true, force: true });
    await search("2020");
    await search("2021");
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
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
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
    await startProbe();
    for (let i = 0; i < 6; i++) await page.getByTestId("search").press("ArrowDown", { delay: 30 });
    await page.waitForTimeout(1500);
    const lag = await stopProbe();
    const sel = await page.locator('[data-testid="results"] tbody tr.sel').innerText();
    const recap = await page.getByTestId("recap").innerText();
    const per = sel.match(/N\d\b/);
    if (per && !recap.includes(`Niveau ${per[0].slice(1)}`)) throw new Error(`aperçu en retard : ligne « ${sel.split("\n").join(" ")} », fiche « ${recap.split("\n")[1]} »`);
    console.log(`    retard maximal pendant le défilement : ${lag} ms`);
    if (lag > 300) throw new Error(`interface figée ${lag} ms`);
  });

  await scenario("Nom avec fautes « tchoa alin » (tolérance)", async () => {
    await search("tchoa alin");
    await page.getByText("Tolérance fautes / phonétique").click();
    await page.locator('[data-testid="results"] tbody tr').first().waitFor({ timeout: 10000 });
    const first = await page.locator('[data-testid="results"] tbody tr').first().innerText();
    if (!/TCHOUA/i.test(first)) throw new Error(`premier résultat : ${first}`);
    await page.getByText("Groupée par étudiant").click();
    await page.waitForTimeout(500);
    await shot("groupee");
    await page.getByText("Par relevé").click();
    await page.getByText("Exacte").click();
  });

  await scenario("Demande multi-années : Ctrl+D ×3, imprimer tout, remettre", async () => {
    await search("18M042");
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
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
    if (!/3 relevés/.test(label)) throw new Error(`barre de demande : ${label}`);
    await page.getByText("Voir / préparer").click();
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
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
    await page.keyboard.press("Control+d");
    await page.getByRole("dialog", { name: "Nouvelle demande" }).waitFor();
    await page.locator('[role="dialog"] input').fill("Sa sœur");
    await page.keyboard.press("Enter");
    await waitToast(/Ajouté à la demande/);
    await search("18M042");
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
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
      opts = await page.getByTestId("demande-picker").locator("option").allInnerTexts();
      if (opts.filter((o) => /KOUAME|TCHOUA/i.test(o)).length >= 2) break;
      await page.waitForTimeout(200);
    }
    if (opts.filter((o) => /KOUAME|TCHOUA/i.test(o)).length < 2) throw new Error(`demandes ouvertes : ${opts.join(" / ")}`);
    const kouame = await page.getByTestId("demande-picker").locator("option", { hasText: "KOUAME" }).first().innerText();
    if (!/\(1\)/.test(kouame)) throw new Error(`la demande de KOUAME a reçu le relevé d'un autre : ${kouame}`);
  });

  await scenario("Cursus : fusion et export, interface jamais figée", async () => {
    await search("18M042");
    await page.locator('[data-testid="results"] tbody tr').first().waitFor();
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

  await scenario("Écran Demandes (Ctrl+2)", async () => {
    await page.keyboard.press("Control+2");
    await page.getByRole("heading", { name: "Demandes de relevés" }).waitFor();
    await page.waitForTimeout(400);
    await shot("demandes");
  });

  await scenario("Extraction Décanat et journal du jour (Ctrl+4)", async () => {
    await page.keyboard.press("Control+4");
    await page.getByRole("heading", { name: "Extraction et journal" }).waitFor();
    await page.waitForTimeout(800);
    const rows = await page.locator("table.grid").last().locator("tbody tr").count();
    if (rows < 3) throw new Error(`journal du jour : ${rows} ligne(s)`);
    await shot("extraction");
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
    await shot("verification");
  });

  await scenario("Indexation (Ctrl+3) : relance différentielle", async () => {
    await page.keyboard.press("Control+3");
    await page.getByTestId("index-start").click();
    await page.getByText("TERMINÉ").first().waitFor({ timeout: 120000 });
    await shot("indexation");
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
    await page.locator('[data-testid="results"] tbody tr').first().waitFor({ timeout: 30000 });
    await shot("apres_relance_moteur");
  });

  await scenario("Réglages : ouverture et enregistrement", async () => {
    await page.getByTestId("settings").click();
    await page.getByRole("dialog", { name: "Réglages" }).waitFor();
    await shot("reglages");
    await page.getByTestId("settings-save").click();
    await waitToast(/Réglages enregistrés/);
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
