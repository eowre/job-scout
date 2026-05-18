const {
  Document, Packer, Paragraph, TextRun, AlignmentType,
  LevelFormat, BorderStyle, HeadingLevel, TabStopType, TabStopPosition
} = require('docx');
const fs = require('fs');

const data = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const outPath = process.argv[3];

// ── Helpers ──────────────────────────────────────────────────────────────────

function sectionRule() {
  return new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "2E75B6", space: 1 } },
    spacing: { after: 80 },
    children: [],
  });
}

function sectionHeading(text) {
  return new Paragraph({
    children: [new TextRun({ text, bold: true, size: 24, color: "2E75B6", font: "Arial" })],
    spacing: { before: 240, after: 60 },
  });
}

function bullet(text) {
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    children: [new TextRun({ text, size: 20, font: "Arial" })],
    spacing: { after: 40 },
  });
}

function spacer(pts = 80) {
  return new Paragraph({ children: [], spacing: { after: pts } });
}

// ── Document content ──────────────────────────────────────────────────────────

const children = [];

// Name
children.push(new Paragraph({
  alignment: AlignmentType.CENTER,
  children: [new TextRun({ text: data.name || "Your Name", bold: true, size: 36, font: "Arial" })],
  spacing: { after: 60 },
}));

// Contact line
if (data.contact) {
  children.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    children: [new TextRun({ text: data.contact, size: 18, color: "555555", font: "Arial" })],
    spacing: { after: 160 },
  }));
}

// Summary
if (data.summary) {
  children.push(sectionHeading("PROFESSIONAL SUMMARY"));
  children.push(sectionRule());
  children.push(new Paragraph({
    children: [new TextRun({ text: data.summary, size: 20, font: "Arial" })],
    spacing: { after: 40 },
  }));
  children.push(spacer());
}

// Experience
if (data.experience && data.experience.length) {
  children.push(sectionHeading("EXPERIENCE"));
  children.push(sectionRule());

  for (const job of data.experience) {
    // Title  |  tab right  |  Dates
    children.push(new Paragraph({
      children: [
        new TextRun({ text: job.title || "", bold: true, size: 22, font: "Arial" }),
        new TextRun({ text: "\t" }),
        new TextRun({ text: job.dates || "", size: 20, color: "555555", font: "Arial" }),
      ],
      tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
      spacing: { after: 40 },
    }));

    // Company
    if (job.company) {
      children.push(new Paragraph({
        children: [new TextRun({ text: job.company, italics: true, size: 20, color: "444444", font: "Arial" })],
        spacing: { after: 60 },
      }));
    }

    for (const b of (job.bullets || [])) {
      children.push(bullet(b));
    }

    children.push(spacer(120));
  }
}

// Education
if (data.education && data.education.length) {
  children.push(sectionHeading("EDUCATION"));
  children.push(sectionRule());

  for (const edu of data.education) {
    children.push(new Paragraph({
      children: [
        new TextRun({ text: edu.degree || "", bold: true, size: 22, font: "Arial" }),
        new TextRun({ text: "\t" }),
        new TextRun({ text: edu.dates || "", size: 20, color: "555555", font: "Arial" }),
      ],
      tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
      spacing: { after: 40 },
    }));
    if (edu.school) {
      children.push(new Paragraph({
        children: [new TextRun({ text: edu.school, italics: true, size: 20, color: "444444", font: "Arial" })],
        spacing: { after: edu.notes ? 40 : 120 },
      }));
    }
    if (edu.notes) {
      children.push(new Paragraph({
        children: [new TextRun({ text: edu.notes, size: 18, color: "555555", font: "Arial" })],
        spacing: { after: 120 },
      }));
    }
  }
}

// Skills
if (data.skills && data.skills.length) {
  children.push(sectionHeading("SKILLS"));
  children.push(sectionRule());
  for (const skill of data.skills) {
    children.push(bullet(skill));
  }
  children.push(spacer());
}

// Extras
if (data.extras && data.extras.length) {
  children.push(sectionHeading("ADDITIONAL"));
  children.push(sectionRule());
  for (const extra of data.extras) {
    children.push(bullet(extra));
  }
}

// ── Build & write ─────────────────────────────────────────────────────────────

const doc = new Document({
  numbering: {
    config: [{
      reference: "bullets",
      levels: [{
        level: 0,
        format: LevelFormat.BULLET,
        text: "•",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 360, hanging: 360 } } },
      }],
    }],
  },
  styles: {
    default: { document: { run: { font: "Arial", size: 20 } } },
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 },
      },
    },
    children,
  }],
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync(outPath, buffer);
  console.log('OK:' + outPath);
}).catch(err => {
  console.error('ERROR:' + err.message);
  process.exit(1);
});
