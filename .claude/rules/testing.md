---
paths:
  - "**/*.test.*"
  - "**/*.spec.*"
  - "tests/**/*"
---

# Testing

- Setiap bug fix disertai test yang mereproduksi bug tersebut.
- Nama test menjelaskan perilaku: `should <hasil> when <kondisi>`.
- Satu test menguji satu perilaku; hindari assertion berlebihan.
- Mock hanya dependensi eksternal (network, DB, waktu), bukan logika internal.
- Jalankan test terkait sebelum menyatakan pekerjaan selesai.
