-- Create a separate database for LiteLLM so its 100+ Prisma migrations
-- never touch the application's hgapp database.
CREATE DATABASE litellm OWNER hgapp;
