// webp_batch.js
// ==============
// Batch image -> WebP converter for site_edit.py's convert_images_to_webp, using
// sharp (libvips) instead of Pillow - roughly 2x faster than Pillow at identical
// quality/effort settings (measured), because libvips' pipeline is more efficient
// than Pillow's for this resize+encode combo.
//
// Usage: node webp_batch.js <jobs.json>
//   jobs.json: [{"src": "...", "dest": "...", "quality": 82, "maxDimension": 2000,
//                "effort": 4}, ...]
// Prints a JSON array of {src, ok, error?} to stdout - one entry per job, in order.

const fs = require("fs");

async function main() {
  const jobsPath = process.argv[2];
  if (!jobsPath) {
    console.error("usage: node webp_batch.js <jobs.json>");
    process.exit(1);
  }
  const sharp = require("sharp");
  const jobs = JSON.parse(fs.readFileSync(jobsPath, "utf-8"));
  const results = [];

  for (const job of jobs) {
    try {
      const maxDim = job.maxDimension || 2000;
      await sharp(job.src)
        .rotate() // apply EXIF orientation before resizing, then strip it
        .resize(maxDim, maxDim, { fit: "inside", withoutEnlargement: true })
        .webp({ quality: job.quality || 82, effort: job.effort ?? 4 })
        .toFile(job.dest);
      results.push({ src: job.src, ok: true });
    } catch (e) {
      results.push({ src: job.src, ok: false, error: String((e && e.message) || e) });
    }
  }

  process.stdout.write(JSON.stringify(results));
}

main();
