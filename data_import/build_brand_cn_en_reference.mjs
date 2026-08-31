import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error(
    "Usage: node build_brand_cn_en_reference.mjs <input.xlsx> <output.json>",
  );
}

const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const sheet = workbook.worksheets.getItemAt(0);
const used = sheet.getUsedRange(true);
const values = used ? used.values : [];
const headers = (values[0] || []).map((value) => String(value ?? "").trim());
if (headers[0] !== "brand_CN" || headers[1] !== "brand_name_DH") {
  throw new Error(
    `Unexpected headers: ${JSON.stringify(headers.slice(0, 2))}`,
  );
}

const candidates = new Map();
let blankChinese = 0;
let blankEnglish = 0;
for (const row of values.slice(1)) {
  const chinese = String(row?.[0] ?? "").trim();
  const english = String(row?.[1] ?? "").trim();
  if (!chinese) blankChinese += 1;
  if (!english) blankEnglish += 1;
  if (!chinese || !english) continue;
  if (!candidates.has(chinese)) candidates.set(chinese, new Set());
  candidates.get(chinese).add(english);
}

const mappings = {};
const conflicts = {};
for (const [chinese, englishNames] of candidates.entries()) {
  const names = [...englishNames];
  if (names.length === 1) mappings[chinese] = names[0];
  else conflicts[chinese] = names;
}

const output = {
  source_file: path.basename(inputPath),
  source_sheet: sheet.name,
  columns: { chinese: "brand_CN", english: "brand_name_DH" },
  stats: {
    data_rows: Math.max(0, values.length - 1),
    unique_chinese: candidates.size,
    unambiguous_mappings: Object.keys(mappings).length,
    conflict_chinese: Object.keys(conflicts).length,
    blank_chinese: blankChinese,
    blank_english: blankEnglish,
  },
  conflicts,
  mappings,
};
await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.writeFile(outputPath, `${JSON.stringify(output, null, 2)}\n`, "utf8");
console.log(JSON.stringify(output.stats));
