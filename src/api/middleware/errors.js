import { ArtifactError } from '../repositories/artifactRepository.js';

export function notFound(req, res) {
  res.set('Cache-Control', 'no-store').status(404).type('application/problem+json').json({ type: 'about:blank', title: 'Not Found', status: 404, detail: 'The requested resource does not exist.', instance: req.originalUrl, requestId: req.requestId });
}

export function errorHandler(error, req, res, next) { // eslint-disable-line no-unused-vars
  const status = error instanceof ArtifactError ? error.status : (Number.isInteger(error.status) ? error.status : 500);
  const title = status === 503 ? 'Service Unavailable' : status === 403 ? 'Forbidden' : status >= 500 ? 'Internal Server Error' : 'Bad Request';
  console.error(JSON.stringify({ event: 'api_error', requestId: req.requestId, status, code: error.code, message: error.message }));
  res.set('Cache-Control', 'no-store').status(status).type('application/problem+json').json({ type: 'about:blank', title, status, ...(error.code ? { code: error.code } : {}), detail: status >= 500 ? 'The requested artifact is temporarily unavailable.' : error.message, instance: req.originalUrl, requestId: req.requestId });
}
