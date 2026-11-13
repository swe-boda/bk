import { handleUpload } from '@vercel/blob/client';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  // Optional admin password check (matches api/index.py)
  const adminPw = process.env.ADMIN_PASSWORD || '';
  if (adminPw) {
    const cookie = req.headers.cookie || '';
    const token = req.headers['x-admin-key'] || '';
    const okCookie = cookie.split(';').some((c) => c.trim() === `admin_pw=${encodeURIComponent(adminPw)}`);
    if (!okCookie && token !== adminPw) {
      return res.status(401).json({ error: 'مطلوب تسجيل الدخول الأول' });
    }
  }

  try {
    const body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body;
    const jsonResponse = await handleUpload({
      body,
      request: req,
      onBeforeGenerateToken: async (pathname) => {
        if (!pathname.startsWith('books/') || !pathname.toLowerCase().endsWith('.pdf')) {
          throw new Error('لازم الملف يكون PDF');
        }
        return {
          allowedContentTypes: ['application/pdf'],
          allowOverwrite: true,
        };
      },
      onUploadCompleted: async () => {},
    });
    return res.status(200).json(jsonResponse);
  } catch (err) {
    return res.status(400).json({ error: err?.message || 'فشل رفع الملف' });
  }
}

