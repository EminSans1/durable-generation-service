"""Guard append-only ledger and seed the fixed catalog, including an empty demo user."""

from alembic import op

revision = "0e72f93a1211"
down_revision = "bf3f4d658e64"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE FUNCTION guard_user_balance() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'INSERT' AND NEW.balance <> 0 THEN
            RAISE EXCEPTION 'initial balance must be zero; insert a ledger payment';
          ELSIF TG_OP = 'UPDATE' AND NEW.balance <> OLD.balance AND pg_trigger_depth() < 2 THEN
            RAISE EXCEPTION 'balance can only change through ledger';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER users_balance_guard BEFORE INSERT OR UPDATE ON users
        FOR EACH ROW EXECUTE FUNCTION guard_user_balance()
    """)
    op.execute("""
        CREATE FUNCTION reject_ledger_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'ledger is append-only';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER ledger_immutable BEFORE UPDATE OR DELETE ON ledger
        FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation()
    """)
    # AFTER INSERT is essential: a conflicting ON CONFLICT insert must NOT change balance.
    op.execute("""
        CREATE FUNCTION apply_ledger_amount() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE owner_id bigint; task_cost integer;
        BEGIN
          IF NEW.task_id IS NOT NULL THEN
            SELECT user_id, cost INTO owner_id, task_cost FROM tasks WHERE id = NEW.task_id;
            IF owner_id IS DISTINCT FROM NEW.user_id
               OR abs(NEW.amount) <> task_cost THEN
              RAISE EXCEPTION 'ledger task owner or amount mismatch';
            END IF;
            IF NEW.kind = 'refund' AND NOT EXISTS (
              SELECT 1 FROM ledger WHERE task_id = NEW.task_id AND kind = 'debit'
            ) THEN
              RAISE EXCEPTION 'refund requires debit';
            END IF;
          END IF;
          UPDATE users SET balance = balance + NEW.amount WHERE id = NEW.user_id;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER ledger_apply AFTER INSERT ON ledger
        FOR EACH ROW EXECUTE FUNCTION apply_ledger_amount()
    """)
    op.execute("INSERT INTO models(slug, cost) VALUES ('image-fast', 2), ('video-long', 25)")
    op.execute("""
        INSERT INTO legs(model_slug, position, provider, max_ref_images, skip_on_moderation)
        VALUES ('image-fast',1,'alpha',4,false), ('image-fast',2,'beta',10,false),
               ('video-long',1,'beta',2,false), ('video-long',2,'alpha',1,true)
    """)
    op.execute("INSERT INTO users(id) VALUES (1)")


def downgrade():
    # Catalog data is removed by the base schema downgrade; never DELETE ledger history here.
    op.execute("DROP TRIGGER ledger_apply ON ledger")
    op.execute("DROP FUNCTION apply_ledger_amount()")
    op.execute("DROP TRIGGER ledger_immutable ON ledger")
    op.execute("DROP FUNCTION reject_ledger_mutation()")
    op.execute("DROP TRIGGER users_balance_guard ON users")
    op.execute("DROP FUNCTION guard_user_balance()")
