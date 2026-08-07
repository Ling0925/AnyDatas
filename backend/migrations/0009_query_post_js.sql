PRAGMA foreign_keys = ON;

-- Optional QuickJS post-process script shared by saved queries, jobs, and schedules.
ALTER TABLE saved_queries ADD COLUMN post_js TEXT;
ALTER TABLE jobs ADD COLUMN post_js TEXT;
ALTER TABLE schedules ADD COLUMN post_js TEXT;
