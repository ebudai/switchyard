-- SYRD-836 (pgu1049): a Director-set test tier on a ticket, `partial` or `full`, with an optional one-line reason.
--
-- Which test tier a ticket needs is the Director's decision per ticket. It was prose in the body, where the
-- implementer or Audit could miss it. It is now a declared instruction kept with the ticket's other Director-only
-- workflow settings: two keys of tickets.workflow_flags, `test_tier` and `test_tier_reason`, written only through
-- set_declared_flags -- the Director's gate write -- so it has the gates' authority check, API and history. Nothing
-- enforces it: the board never runs or verifies tests.
--
-- - set_declared_flags takes `test_tier` ("partial", "full", or null to clear) and, with it, an optional
--   `test_tier_reason`. Each change is a ticket comment by the Director and a ticket_field_audit row. A patch with
--   no tier key behaves exactly as before.
-- - ticket_test_tier(ticket) is the listener's read of it, for the hand-off line it adds to a transition notice.
--
-- schema.sql carries the same functions; rbac.sql grants ticket_test_tier to the listener alone.

BEGIN;

CREATE OR REPLACE FUNCTION ticket_board.set_declared_flags(id text, patch jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=ticket_board,pg_temp AS $$
DECLARE cfg jsonb:=ticket_board.declared_workflow(); t ticket_board.tickets; item record; doc jsonb;
 tiered boolean; gates jsonb; tier text; reason text; was jsonb; became jsonb;
BEGIN
 IF ticket_board.current_actor_role()<>'ticket_board_service' OR ticket_board.current_app_actor() IS DISTINCT FROM 'director' THEN RAISE EXCEPTION 'only director may set gates' USING ERRCODE='42501'; END IF;
 IF jsonb_typeof(patch) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'configured gates object required'; END IF;
 -- SYRD-836: the test tier is not a declared gate, and needs no declared workflow to be set.
 tiered:=patch ?| ARRAY['test_tier','test_tier_reason'];
 gates:=patch-'test_tier'-'test_tier_reason';
 IF cfg IS NULL AND (gates<>'{}'::jsonb OR NOT tiered) THEN RAISE EXCEPTION 'configured gates object required'; END IF;
 SELECT * INTO STRICT t FROM ticket_board.tickets WHERE tickets.id=set_declared_flags.id FOR UPDATE;
 doc:=to_jsonb(t);
 FOR item IN SELECT * FROM jsonb_each(gates) LOOP
  IF cfg->'flags'->item.key->>'kind' IS DISTINCT FROM 'gate' OR jsonb_typeof(item.value) IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'only declared boolean gates may be edited'; END IF;
  doc:=ticket_board.set_workflow_flag(doc,item.key,(item.value::text)::boolean);
 END LOOP;
 t:=jsonb_populate_record(t,doc);
 IF tiered THEN
  IF NOT patch ? 'test_tier' OR jsonb_typeof(patch->'test_tier') NOT IN ('string','null')
     OR (patch ? 'test_tier_reason' AND jsonb_typeof(patch->'test_tier_reason') IS DISTINCT FROM 'string') THEN
   RAISE EXCEPTION 'a test tier is "partial", "full" or null, with an optional text reason';
  END IF;
  tier:=patch->>'test_tier';
  reason:=btrim(coalesce(patch->>'test_tier_reason',''));
  IF tier IS NOT NULL AND tier NOT IN ('partial','full') THEN RAISE EXCEPTION 'a test tier is partial or full, not %', tier; END IF;
  IF tier IS NULL AND reason<>'' THEN RAISE EXCEPTION 'a cleared test tier takes no reason'; END IF;
  IF length(reason)>300 OR reason ~ '[[:cntrl:]]' THEN RAISE EXCEPTION 'a test tier reason is one line of at most 300 characters'; END IF;
  was:=jsonb_strip_nulls(jsonb_build_object('tier',t.workflow_flags->'test_tier','reason',t.workflow_flags->'test_tier_reason'));
  t.workflow_flags:=(t.workflow_flags-'test_tier'-'test_tier_reason')
   ||CASE WHEN tier IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('test_tier',tier) END
   ||CASE WHEN reason='' THEN '{}'::jsonb ELSE jsonb_build_object('test_tier_reason',reason) END;
  became:=jsonb_strip_nulls(jsonb_build_object('tier',tier,'reason',nullif(reason,'')));
 END IF;
 UPDATE ticket_board.tickets SET needs_inspection=t.needs_inspection,needs_audit=t.needs_audit,needs_user_signoff=t.needs_user_signoff,workflow_flags=t.workflow_flags WHERE tickets.id=t.id;
 IF gates<>'{}'::jsonb OR NOT tiered THEN
  PERFORM ticket_board.append_ticket_comment(t.id,'director','Updated workflow gates: '||gates::text);
 END IF;
 IF tiered THEN
  INSERT INTO ticket_board.ticket_field_audit (ticket_id, actor, reason, field, old_value, new_value)
  VALUES (t.id, 'director', coalesce(nullif(reason,''), 'test tier set by the Director'), 'test_tier',
          nullif(was,'{}'::jsonb), nullif(became,'{}'::jsonb));
  PERFORM ticket_board.append_ticket_comment(t.id,'director',
   CASE WHEN tier IS NULL THEN 'Cleared the test tier'||coalesce(' (was '||(was->>'tier')||')','')
   ELSE 'Set the test tier to '||tier||coalesce(' (was '||(was->>'tier')||')','')
        ||CASE WHEN reason<>'' THEN ': '||reason ELSE '' END END);
 END IF;
END;
$$;

-- SYRD-836: the ticket's test tier, for the listener's hand-off line. Null when it has none.
CREATE OR REPLACE FUNCTION ticket_board.ticket_test_tier(p_ticket text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = ticket_board, pg_temp AS $$
BEGIN
    PERFORM ticket_board.require_ticket_board_listener('ticket_test_tier');
    RETURN (SELECT CASE WHEN t.workflow_flags->>'test_tier' IN ('partial', 'full') THEN
                   jsonb_build_object('tier', t.workflow_flags->>'test_tier',
                                      'reason', coalesce(t.workflow_flags->>'test_tier_reason', '')) END
            FROM ticket_board.tickets t WHERE t.id = p_ticket);
END;
$$;
REVOKE ALL ON FUNCTION ticket_board.ticket_test_tier(text) FROM PUBLIC;

COMMIT;
