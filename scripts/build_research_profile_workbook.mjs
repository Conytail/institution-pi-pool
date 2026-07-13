import fs from "node:fs/promises";
import path from "node:path";

import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = process.cwd();
const base = path.join(root, "outputs", "research_profile_eval_20260712");
const outputPath = path.join(base, "research_profile_resolution_benchmark.xlsx");
const previewDir = path.join(base, "previews");

const COLORS = {
  navy: "#17324D",
  teal: "#0F766E",
  amber: "#B7791F",
  ink: "#1F2937",
  muted: "#5B6573",
  line: "#D6DCE3",
  paleBlue: "#EAF2F8",
  paleTeal: "#E7F4F1",
  paleAmber: "#FFF4D6",
  paleGray: "#F3F5F7",
  white: "#FFFFFF",
};

function columnName(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

function displayHeader(value) {
  const special = {
    config_id: "Config ID",
    selected_config_id: "Selected Config ID",
    pi_count: "PI Count",
    mrr: "MRR",
    openalex_author_id: "OpenAlex Author ID",
    openalex_orcid: "OpenAlex ORCID",
    ndcg_at_10: "nDCG@10",
    mean_ndcg_at_10: "Mean nDCG@10",
    worst_ndcg_at_10: "Worst nDCG@10",
    max_ndcg_regret: "Max nDCG Regret",
  };
  if (special[value]) return special[value];
  return value
    .split("_")
    .map((part) => {
      if (part === "ndcg") return "nDCG";
      if (part === "mrr") return "MRR";
      if (part === "pi") return "PI";
      if (part === "tfidf") return "TF-IDF";
      if (part === "openalex") return "OpenAlex";
      return part ? part[0].toUpperCase() + part.slice(1) : part;
    })
    .join(" ");
}

const TEXT_HEADERS = new Set([
  "config_id",
  "selected_config_id",
  "mode",
  "encoder",
  "cluster_count",
  "selected_cluster_count",
  "paper_scope",
  "selected_paper_scope",
  "heldout_institution",
  "institution_name",
  "selected_mode",
]);

function convertCsvValue(header, value) {
  if (value === "") return null;
  if (header === "robust_one_se_eligible") return value.toLowerCase() === "true";
  if (!TEXT_HEADERS.has(header) && /^-?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(value)) {
    return Number(value);
  }
  return value;
}

async function loadCsv(filePath) {
  const csvText = await fs.readFile(filePath, "utf8");
  const temporary = await Workbook.fromCSV(csvText, { sheetName: "Imported" });
  const values = temporary.worksheets.getItem("Imported").getUsedRange().values;
  const headers = values[0].map((value) => String(value));
  const rows = values.slice(1).map((row) =>
    row.map((value, index) => convertCsvValue(headers[index], value == null ? "" : String(value))),
  );
  return { headers, rows };
}

function widthForHeader(header) {
  if (header.includes("institution")) return 30;
  if (header.includes("config_id")) return 17;
  if (header.includes("mode")) return 24;
  if (header.includes("scope")) return 18;
  if (header.includes("bytes")) return 18;
  if (header.includes("ndcg") || header.includes("recall") || header === "mrr") return 16;
  if (header.includes("multiplier")) return 18;
  if (header.includes("openalex")) return 28;
  return Math.min(20, Math.max(11, header.length + 2));
}

function formatDataColumns(sheet, headers, rowCount) {
  headers.forEach((header, index) => {
    const column = sheet.getRangeByIndexes(0, index, rowCount + 1, 1);
    column.format.columnWidth = widthForHeader(header);
    if (!rowCount) return;
    const data = sheet.getRangeByIndexes(1, index, rowCount, 1);
    if (header.includes("bytes") || header.endsWith("_count") || header === "cases") {
      data.setNumberFormat("#,##0");
    } else if (header.includes("multiplier")) {
      data.setNumberFormat('0.00"x"');
    } else if (header.includes("ndcg") || header.includes("recall") || header === "mrr") {
      data.setNumberFormat("0.0000");
    } else if (header.includes("_ms_")) {
      data.setNumberFormat("0.000");
    } else if (header === "mean_rank" || header === "heldout_mean_rank") {
      data.setNumberFormat("0.00");
    }
  });
}

function addDataSheet(workbook, name, tableName, data, options = {}) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const matrix = [data.headers.map(displayHeader), ...data.rows];
  sheet.getRangeByIndexes(0, 0, matrix.length, matrix[0].length).values = matrix;
  const finalColumn = columnName(matrix[0].length - 1);
  const table = sheet.tables.add(`A1:${finalColumn}${matrix.length}`, true, tableName);
  table.style = "TableStyleMedium2";
  sheet.freezePanes.freezeRows(1);
  const header = sheet.getRangeByIndexes(0, 0, 1, matrix[0].length);
  header.format.fill = COLORS.navy;
  header.format.font = { bold: true, color: COLORS.white, size: 10 };
  header.format.wrapText = true;
  header.format.rowHeight = 32;
  header.format.verticalAlignment = "center";
  formatDataColumns(sheet, data.headers, data.rows.length);
  if (options.highlightConfigIds?.length) {
    const idIndex = data.headers.indexOf("config_id");
    data.rows.forEach((row, rowIndex) => {
      if (options.highlightConfigIds.includes(row[idIndex])) {
        sheet.getRangeByIndexes(rowIndex + 1, 0, 1, matrix[0].length).format.fill = COLORS.paleTeal;
        sheet.getRangeByIndexes(rowIndex + 1, 0, 1, matrix[0].length).format.font = {
          bold: true,
          color: COLORS.ink,
        };
      }
    });
  }
  const robustIndex = data.headers.indexOf("robust_one_se_eligible");
  if (robustIndex >= 0) {
    data.rows.forEach((row, rowIndex) => {
      if (row[robustIndex] === true) {
        sheet.getCell(rowIndex + 1, robustIndex).format.fill = COLORS.paleTeal;
        sheet.getCell(rowIndex + 1, robustIndex).format.font = { bold: true, color: COLORS.teal };
      }
    });
  }
  return sheet;
}

const cross = await loadCsv(path.join(base, "comparison", "cross_encoder_results.csv"));
const production = await loadCsv(path.join(base, "production_terms", "config_results.csv"));
const tfidf = await loadCsv(path.join(base, "tfidf", "config_results.csv"));
const productionLoso = await loadCsv(
  path.join(base, "production_terms", "leave_one_institution_out.csv"),
);
const tfidfLoso = await loadCsv(path.join(base, "tfidf", "leave_one_institution_out.csv"));
const paperAblation = await loadCsv(path.join(base, "comparison", "publication_ablation.csv"));
const productionManifest = JSON.parse(
  await fs.readFile(path.join(base, "production_terms", "experiment_manifest.json"), "utf8"),
);
const tfidfManifest = JSON.parse(
  await fs.readFile(path.join(base, "tfidf", "experiment_manifest.json"), "utf8"),
);
const comparisonManifest = JSON.parse(
  await fs.readFile(path.join(base, "comparison", "comparison_manifest.json"), "utf8"),
);
const dataset = JSON.parse(
  await fs.readFile(path.join(base, "data", "research_profile_dataset.json"), "utf8"),
);

const robustConfigId = comparisonManifest.robust_selected_config.config_id;
const productionConfigId = productionManifest.selected_config.config_id;
const tfidfConfigId = tfidfManifest.selected_config.config_id;

function excelRowForConfig(data, configId) {
  const idIndex = data.headers.indexOf("config_id");
  const rowIndex = data.rows.findIndex((row) => row[idIndex] === configId);
  if (rowIndex < 0) throw new Error(`config not found: ${configId}`);
  return rowIndex + 2;
}

const robustCrossRow = excelRowForConfig(cross, robustConfigId);
const productionSelectedRow = excelRowForConfig(production, productionConfigId);
const tfidfSelectedRow = excelRowForConfig(tfidf, tfidfConfigId);

const workbook = Workbook.create();
const summary = workbook.worksheets.add("Summary");
summary.showGridLines = false;

addDataSheet(workbook, "Cross Encoder", "CrossEncoderConfigs", cross, {
  highlightConfigIds: [robustConfigId],
});
addDataSheet(workbook, "Production", "ProductionConfigs", production, {
  highlightConfigIds: [productionConfigId, robustConfigId],
});
addDataSheet(workbook, "TFIDF", "TfidfConfigs", tfidf, {
  highlightConfigIds: [tfidfConfigId, robustConfigId],
});

const loso = {
  headers: ["encoder", ...productionLoso.headers],
  rows: [
    ...productionLoso.rows.map((row) => ["production_terms", ...row]),
    ...tfidfLoso.rows.map((row) => ["tfidf", ...row]),
  ],
};
addDataSheet(workbook, "LOSO", "LeaveOneInstitutionOut", loso);
addDataSheet(workbook, "Paper Ablation", "PublicationAblation", paperAblation);

const piData = {
  headers: [
    "institution_name",
    "display_name",
    "title",
    "research_areas",
    "person_id",
    "openalex_author_id",
    "openalex_orcid",
    "identity_score",
    "openalex_works_count",
    "retained_works_count",
  ],
  rows: [...dataset.pis]
    .sort((left, right) =>
      `${left.institution_name}|${left.display_name}`.localeCompare(
        `${right.institution_name}|${right.display_name}`,
      ),
    )
    .map((pi) => [
      pi.institution_name,
      pi.display_name,
      pi.title,
      (pi.research_areas || []).join("; "),
      pi.person_id,
      pi.openalex_author_id,
      pi.openalex_orcid,
      pi.identity_score,
      pi.openalex_works_count,
      pi.works.length,
    ]),
};
const quality = addDataSheet(workbook, "Data Quality", "IdentityLinkedPIs", piData);
quality.getRange(`C2:G${piData.rows.length + 1}`).format.wrapText = true;
quality.getRange(`C2:G${piData.rows.length + 1}`).format.rowHeight = 52;
quality.getRange(`A2:J${piData.rows.length + 1}`).format.verticalAlignment = "center";
quality.getRange(`C1:C${piData.rows.length + 1}`).format.columnWidth = 25;
quality.getRange(`D1:D${piData.rows.length + 1}`).format.columnWidth = 42;
quality.getRange(`E1:E${piData.rows.length + 1}`).format.columnWidth = 20;
quality.getRange(`F1:G${piData.rows.length + 1}`).format.columnWidth = 34;

const protocol = workbook.worksheets.add("Protocol");
protocol.showGridLines = false;
protocol.getRange("A1:D1").merge();
protocol.getRange("A1").values = [["Research Profile Resolution Experiment Protocol"]];
protocol.getRange("A1:D1").format.fill = COLORS.navy;
protocol.getRange("A1:D1").format.font = { bold: true, color: COLORS.white, size: 18 };
protocol.getRange("A1:D1").format.rowHeight = 34;
protocol.getRange("A2:D2").merge();
protocol.getRange("A2").values = [[
  "Parameter selection measures institution-constrained research retrieval; identity and supervisor eligibility remain separate layers.",
]];
protocol.getRange("A2:D2").format.font = { italic: true, color: COLORS.muted, size: 10 };
protocol.getRange("A2:D2").format.wrapText = true;
protocol.getRange("A4:D4").values = [["Area", "Control", "Implementation", "Status"]];
const protocolRows = [
  ["Candidate universe", "Institution hard gate", "Candidates come only from the official university PI pool.", "Applied"],
  ["Identity", "Verified author link", "Near-exact name plus matching official-institution ROR during benchmark preparation.", "Applied"],
  ["Leakage", "Global publication removal", "Every proposal/CV holdout work is removed from all coauthor profiles.", "Applied"],
  ["Queries", "Applicant-like split", "Proposal uses held-out recent work; CV uses separate held-out works.", "Applied"],
  ["Relevance", "Multi-positive", "All verified in-pool coauthors of the held-out work are relevant.", "Applied"],
  ["Uncertainty", "PI-clustered SE", "Two cases from one PI do not count as two independent applicants.", "Applied"],
  ["Robustness", "Leave one institution out", "Parameters are selected on two institutions and evaluated on the third.", "Applied"],
  ["Encoder", "Two frozen encoders", "Production term weights and corpus TF-IDF use identical cases/configurations.", "Applied"],
  ["Cost", "One-SE rule", "Choose minimum Research Profile bytes inside nDCG and Recall@5 quality floors.", "Applied"],
  ["Paper backtrace", "Feature, not gate", "Final research score uses max(profile semantic, paper feature); zero paper match does not filter.", "Applied"],
  ["External test", "Human relevance labels", "Requires exhaustive official institution pools and unseen applicant labels.", "Pending"],
  ["Available applicants", "54 virtual slots", "Local fixed_slot_001..054 records are empty placeholders; only two real packets exist.", "Missing input"],
];
protocol.getRangeByIndexes(4, 0, protocolRows.length, 4).values = protocolRows;
protocol.getRange(`A4:D${protocolRows.length + 4}`).format.borders = {
  preset: "all",
  style: "thin",
  color: COLORS.line,
};
protocol.getRange("A4:D4").format.fill = COLORS.teal;
protocol.getRange("A4:D4").format.font = { bold: true, color: COLORS.white };
protocol.getRange(`A5:D${protocolRows.length + 4}`).format.wrapText = true;
protocol.getRange(`A5:D${protocolRows.length + 4}`).format.rowHeight = 38;
protocol.getRange("A1:A20").format.columnWidth = 20;
protocol.getRange("B1:B20").format.columnWidth = 24;
protocol.getRange("C1:C20").format.columnWidth = 72;
protocol.getRange("D1:D20").format.columnWidth = 16;
protocol.freezePanes.freezeRows(4);

const chartData = workbook.worksheets.add("Chart Data");
chartData.showGridLines = false;
chartData.getRange("A1:D1").values = [[
  "Other Profile KB",
  "Other Mean nDCG@10",
  "Robust-Eligible Profile KB",
  "Robust-Eligible Mean nDCG@10",
]];
chartData.getRange("A1:D1").format.fill = COLORS.navy;
chartData.getRange("A1:D1").format.font = { bold: true, color: COLORS.white };
const crossProfileIndex = cross.headers.indexOf("mean_research_profile_bytes_per_pi");
const crossNdcgIndex = cross.headers.indexOf("mean_ndcg_at_10");
const crossEligibleIndex = cross.headers.indexOf("robust_one_se_eligible");
const chartCandidates = cross.rows
  .map((row, index) => ({ row, excelRow: index + 2 }))
  .filter(({ row }) => row[crossProfileIndex] <= 40 * 1024);
const otherCandidates = chartCandidates.filter(({ row }) => row[crossEligibleIndex] !== true);
const eligibleCandidates = chartCandidates.filter(({ row }) => row[crossEligibleIndex] === true);
const otherFormulas = otherCandidates.map(({ excelRow }) => [
  `='Cross Encoder'!L${excelRow}/1024`,
  `='Cross Encoder'!H${excelRow}`,
]);
const eligibleFormulas = eligibleCandidates.map(({ excelRow }) => [
  `='Cross Encoder'!L${excelRow}/1024`,
  `='Cross Encoder'!H${excelRow}`,
]);
chartData.getRangeByIndexes(1, 0, otherFormulas.length, 2).formulas = otherFormulas;
chartData.getRangeByIndexes(1, 2, eligibleFormulas.length, 2).formulas = eligibleFormulas;
chartData.getRange(`A2:D${Math.max(otherFormulas.length, eligibleFormulas.length) + 1}`).setNumberFormat(
  "0.000",
);
chartData.getRange("A1:D80").format.columnWidth = 24;
chartData.freezePanes.freezeRows(1);

summary.getRange("A1:Q1").merge();
summary.getRange("A1").values = [["Research Profile Storage Resolution Benchmark"]];
summary.getRange("A1:Q1").format.fill = COLORS.navy;
summary.getRange("A1:Q1").format.font = { bold: true, color: COLORS.white, size: 20 };
summary.getRange("A1:Q1").format.rowHeight = 38;
summary.getRange("A2:Q2").merge();
summary.getRange("A2").values = [[
  "Pilot result: 36 verified PIs, 3 official institution pools, 2,879 works, 72 leakage-controlled cases, and 72 profile configurations.",
]];
summary.getRange("A2:Q2").format.font = { color: COLORS.muted, italic: true, size: 10 };

summary.getRange("A4:G4").merge();
summary.getRange("A4").values = [["Recommended Defaults"]];
summary.getRange("A4:G4").format.fill = COLORS.teal;
summary.getRange("A4:G4").format.font = { bold: true, color: COLORS.white, size: 12 };
summary.getRange("A5:G5").values = [[
  "Decision Scope",
  "Representation",
  "Features",
  "Recent Window",
  "Profile Bytes / PI",
  "nDCG@10",
  "Recall@5",
]];
summary.getRange("A5:G5").format.fill = COLORS.paleBlue;
summary.getRange("A5:G5").format.font = { bold: true, color: COLORS.ink };
summary.getRange("A6:A9").values = [[
  "Cross-encoder conservative",
], ["Current production encoder"], ["Best raw production accuracy"], ["TF-IDF baseline"]];
summary.getRange("B6:G6").formulas = [[
  `='Cross Encoder'!B${robustCrossRow}`,
  `='Cross Encoder'!C${robustCrossRow}`,
  `='Cross Encoder'!D${robustCrossRow}`,
  `='Cross Encoder'!L${robustCrossRow}`,
  `='Cross Encoder'!H${robustCrossRow}`,
  `='Cross Encoder'!K${robustCrossRow}`,
]];
summary.getRange("B7:G7").formulas = [[
  `='Production'!C${productionSelectedRow}`,
  `='Production'!D${productionSelectedRow}`,
  `='Production'!E${productionSelectedRow}`,
  `='Production'!V${productionSelectedRow}`,
  `='Production'!K${productionSelectedRow}`,
  `='Production'!N${productionSelectedRow}`,
]];
summary.getRange("B8:G8").formulas = [[
  "='Production'!C2",
  "='Production'!D2",
  "='Production'!E2",
  "='Production'!V2",
  "='Production'!K2",
  "='Production'!N2",
]];
summary.getRange("B9:G9").formulas = [[
  `='TFIDF'!C${tfidfSelectedRow}`,
  `='TFIDF'!D${tfidfSelectedRow}`,
  `='TFIDF'!E${tfidfSelectedRow}`,
  `='TFIDF'!V${tfidfSelectedRow}`,
  `='TFIDF'!K${tfidfSelectedRow}`,
  `='TFIDF'!N${tfidfSelectedRow}`,
]];
summary.getRange("A6:G9").format.borders = { preset: "all", style: "thin", color: COLORS.line };
summary.getRange("A6:G6").format.fill = COLORS.paleTeal;
summary.getRange("A6:G6").format.font = { bold: true, color: COLORS.ink };
summary.getRange("E6:E9").setNumberFormat("#,##0");
summary.getRange("F6:G9").setNumberFormat("0.0000");

summary.getRange("A12:G12").merge();
summary.getRange("A12").values = [["Storage Boundary"]];
summary.getRange("A12:G12").format.fill = COLORS.amber;
summary.getRange("A12:G12").format.font = { bold: true, color: COLORS.white, size: 12 };
summary.getRange("A13:D13").values = [["Layer", "Purpose", "Bytes / PI", "Default"]];
summary.getRange("A13:D13").format.fill = COLORS.paleAmber;
summary.getRange("A13:D13").format.font = { bold: true, color: COLORS.ink };
summary.getRange("A14:B16").values = [
  ["Publication manifest", "Work ID / DOI / year / title fingerprint for refresh and provenance"],
  ["Research Profile", "Single 256-feature career vector for encoder-robust retrieval"],
  ["Combined baseline", "Manifest plus retrieval profile"],
];
summary.getRange("C14:C16").formulas = [[
  `='Cross Encoder'!M${robustCrossRow}-'Cross Encoder'!L${robustCrossRow}`,
], [
  `='Cross Encoder'!L${robustCrossRow}`,
], [
  `='Cross Encoder'!M${robustCrossRow}`,
]];
summary.getRange("D14:D16").values = [["Yes"], ["Yes"], ["14.4 KB / PI"]];
summary.getRange("A14:D16").format.borders = { preset: "all", style: "thin", color: COLORS.line };
summary.getRange("C14:C16").setNumberFormat("#,##0");
summary.getRange("B14:B16").format.wrapText = true;

summary.getRange("A19:G19").merge();
summary.getRange("A19").values = [["Publication-Vector Ablation"]];
summary.getRange("A19:G19").format.fill = COLORS.navy;
summary.getRange("A19:G19").format.font = { bold: true, color: COLORS.white, size: 12 };
summary.getRange("A20:F20").values = [[
  "Encoder",
  "Paper Scope",
  "Mean nDCG Delta",
  "Mean Recall@5 Delta",
  "Profile Storage",
  "Decision",
]];
summary.getRange("A20:F20").format.fill = COLORS.paleBlue;
summary.getRange("A20:F20").format.font = { bold: true, color: COLORS.ink };
summary.getRange("A20:F20").format.wrapText = true;
summary.getRange("A20:F20").format.rowHeight = 34;
for (let index = 0; index < paperAblation.rows.length; index += 1) {
  const sourceRow = index + 2;
  const targetRow = index + 21;
  summary.getRange(`A${targetRow}:E${targetRow}`).formulas = [[
    `='Paper Ablation'!A${sourceRow}`,
    `='Paper Ablation'!B${sourceRow}`,
    `='Paper Ablation'!D${sourceRow}`,
    `='Paper Ablation'!G${sourceRow}`,
    `='Paper Ablation'!H${sourceRow}`,
  ]];
  summary.getRange(`F${targetRow}`).values = [[
    paperAblation.rows[index][1] === "all" ? "Do not store by default" : "Optional rerank cache only",
  ]];
}
summary.getRange("A21:F24").format.borders = { preset: "all", style: "thin", color: COLORS.line };
summary.getRange("C21:D24").setNumberFormat("0.0000;[Red]-0.0000");
summary.getRange("E21:E24").setNumberFormat('0.00"x"');

summary.getRange("A27:G27").merge();
summary.getRange("A27").values = [["Validity Boundary"]];
summary.getRange("A27:G27").format.fill = COLORS.teal;
summary.getRange("A27:G27").format.font = { bold: true, color: COLORS.white, size: 12 };
summary.getRange("A28:B33").values = [
  ["Verified PIs", dataset.resolved_pi_count],
  ["Institutions", 3],
  ["Held-out cases", productionManifest.case_count],
  ["OpenAlex works", dataset.pis.reduce((sum, pi) => sum + pi.works.length, 0)],
  ["Usable real applicant packets", 2],
  ["Missing virtual slots", 54],
];
summary.getRange("D28:G33").merge(true);
summary.getRange("D28").values = [["Pilot, not a permanent global optimum"]];
summary.getRange("D29").values = [["Candidate pools contain only 9-14 publication-linked PIs."]];
summary.getRange("D30").values = [["The two real packets are not exhaustive institution-pool benchmarks."]];
summary.getRange("D31").values = [["Final validation needs populated CV/proposal slots and graded PI labels."]];
summary.getRange("D32").values = [["Repeat this ablation whenever the production encoder changes."]];
summary.getRange("D33").values = [["Supervisor validity remains an independent score/filter."]];
summary.getRange("A28:B33").format.borders = { preset: "all", style: "thin", color: COLORS.line };
summary.getRange("A28:A33").format.font = { bold: true, color: COLORS.ink };
summary.getRange("D28:G33").format.fill = COLORS.paleGray;
summary.getRange("D28:G33").format.wrapText = true;
summary.getRange("D28:G33").format.font = { color: COLORS.muted };

const chart = summary.charts.add("scatter", {
  chartType: "scatter",
  title: "Accuracy vs Research Profile storage (<=40 KB/PI)",
  hasLegend: true,
});
const otherSeries = chart.series.add("Other tested configs");
otherSeries.categoryFormula = `'Chart Data'!$A$2:$A$${otherFormulas.length + 1}`;
otherSeries.formula = `'Chart Data'!$B$2:$B$${otherFormulas.length + 1}`;
otherSeries.fill = "#94A3B8";
const eligibleSeries = chart.series.add("Cross-encoder one-SE eligible");
eligibleSeries.categoryFormula = `'Chart Data'!$C$2:$C$${eligibleFormulas.length + 1}`;
eligibleSeries.formula = `'Chart Data'!$D$2:$D$${eligibleFormulas.length + 1}`;
eligibleSeries.fill = COLORS.teal;
chart.setPosition("I4", "Q20");
chart.title = "Accuracy vs Research Profile storage (<=40 KB/PI)";
chart.titleTextStyle.fontSize = 12;
chart.hasLegend = true;
chart.xAxis = { numberFormatCode: "0.0", min: 0, max: 40, textStyle: { fontSize: 9 } };
chart.yAxis = { numberFormatCode: "0.000", min: 0.84, max: 0.92, textStyle: { fontSize: 9 } };
chart.xAxis.title.text = "Research Profile KB / PI";
chart.yAxis.title.text = "Mean nDCG@10";

summary.getRange("A1:A40").format.columnWidth = 29;
summary.getRange("B1:B40").format.columnWidth = 27;
summary.getRange("C1:C40").format.columnWidth = 20;
summary.getRange("D1:D40").format.columnWidth = 22;
summary.getRange("E1:E40").format.columnWidth = 18;
summary.getRange("F1:F40").format.columnWidth = 25;
summary.getRange("G1:G40").format.columnWidth = 18;
summary.getRange("H1:H40").format.columnWidth = 3;
summary.getRange("I1:Q40").format.columnWidth = 12;
summary.getRange("A1:Q40").format.font = { name: "Aptos", color: COLORS.ink, size: 10 };
summary.getRange("A1:Q1").format.font = {
  name: "Aptos",
  bold: true,
  color: COLORS.white,
  size: 20,
};
summary.getRange("A2:Q2").format.font = {
  name: "Aptos",
  color: COLORS.muted,
  italic: true,
  size: 10,
};
for (const sectionRange of ["A4:G4", "A12:G12", "A19:G19", "A27:G27"]) {
  summary.getRange(sectionRange).format.font = {
    name: "Aptos",
    bold: true,
    color: COLORS.white,
    size: 12,
  };
}
summary.getRange("A4:G33").format.verticalAlignment = "center";
summary.freezePanes.freezeRows(2);

await fs.mkdir(previewDir, { recursive: true });
const sheetNames = [
  "Summary",
  "Cross Encoder",
  "Production",
  "TFIDF",
  "LOSO",
  "Paper Ablation",
  "Data Quality",
  "Protocol",
  "Chart Data",
];
for (const sheetName of sheetNames) {
  const preview = await workbook.render({
    sheetName,
    autoCrop: "all",
    scale: sheetName === "Summary" ? 1 : 0.65,
    format: "png",
  });
  const fileName = `${sheetName.toLowerCase().replaceAll(" ", "_")}.png`;
  await fs.writeFile(path.join(previewDir, fileName), new Uint8Array(await preview.arrayBuffer()));
}

const formulaInspection = await workbook.inspect({
  kind: "formula",
  sheetId: "Summary",
  range: "A1:Q33",
  maxChars: 6000,
  options: { maxResults: 100 },
});
const drawingInspection = await workbook.inspect({
  kind: "drawing",
  sheetId: "Summary",
  maxChars: 3000,
});
const workbookInspection = await workbook.inspect({
  kind: "sheet,table",
  maxChars: 6000,
  tableMaxRows: 3,
  tableMaxCols: 8,
});

await fs.mkdir(base, { recursive: true });
const exported = await SpreadsheetFile.exportXlsx(workbook);
await exported.save(outputPath);

console.log(
  JSON.stringify(
    {
      outputPath,
      previewDir,
      sheetNames,
      robustConfigId,
      productionConfigId,
      tfidfConfigId,
      formulaInspection: formulaInspection.ndjson,
      drawingInspection: drawingInspection.ndjson,
      workbookInspection: workbookInspection.ndjson,
    },
    null,
    2,
  ),
);
