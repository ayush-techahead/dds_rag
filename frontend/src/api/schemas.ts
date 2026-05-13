import { z } from 'zod';

/** POST /api/v1/auth/login JSON body */
export const loginRequestSchema = z.object({
  email: z.string().trim().email(),
  password: z.string().min(1).max(72),
});

/** POST /api/v1/chat/sessions */
export const createSessionBodySchema = z.object({
  title: z.string().max(120).nullable().optional(),
});

/** POST .../messages/stream body */
export const streamMessageBodySchema = z.object({
  content: z.string().min(1).max(8000),
});
