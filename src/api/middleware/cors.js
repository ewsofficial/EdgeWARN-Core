import cors from 'cors';

export function createCors(allowedOrigins, policy) {
  const allowed = new Set(allowedOrigins);
  const allowAll = allowed.has('*');
  return cors({
    origin(origin, callback) {
      if (!origin) return callback(null, false);
      if (allowAll) return callback(null, '*');
      if (allowed.has(origin)) return callback(null, origin);
      const error = new Error('Origin is not allowed by CORS policy');
      error.status = 403;
      error.code = 'CORS_ORIGIN_DENIED';
      return callback(error);
    },
    credentials: policy.credentials,
    methods: policy.methods,
    allowedHeaders: policy.allowed_headers,
    maxAge: policy.max_age
  });
}
