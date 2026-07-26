GRANT USAGE, CREATE ON SCHEMA public TO brain_app;
GRANT USAGE, CREATE ON SCHEMA provisional TO brain_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO brain_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA provisional TO brain_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO brain_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA provisional TO brain_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO brain_app;
ALTER DEFAULT PRIVILEGES FOR ROLE brain IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO brain_app;
ALTER DEFAULT PRIVILEGES FOR ROLE brain IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO brain_app;
ALTER DEFAULT PRIVILEGES FOR ROLE brain IN SCHEMA provisional GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO brain_app;

-- Least privilege (least-DELETE): the blanket grant above hands the app DELETE on every table, but the
-- app never HARD-deletes these — memory is soft-deleted via UPDATE(deleted_at); agent/enrollment/
-- enrollment_approval/lesson/message/proposal are update-only or append-only (verified against every
-- `DELETE FROM` in the app). Drop DELETE there. (migration 0032 does the same for existing installs,
-- which don't re-run this file.) task/project/idea keep DELETE — RLS narrows it to managers.
REVOKE DELETE ON memory, agent, enrollment, enrollment_approval, lesson, message, proposal FROM brain_app;
