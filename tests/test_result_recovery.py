"""Integration cases that catch misattribution when an owner turn is replayed."""
from unittest.mock import patch
from test_architecture import Workspace, final, PROJECT_OWNER, OWNER_TURN
from alfred_local import finish


class ResultRecovery(Workspace):
    async def pair(self):
        await self.elected(update=1, brief="Check the hours.", call="audit")
        await self.elected(update=2, brief="Return the game build.", call="game")
        return [row[0] for row in self.inbox.db.execute(
            "SELECT route_id FROM appserver_project_routes ORDER BY created")]

    def job(self, route_id):
        return tuple(self.inbox.db.execute("SELECT state,outcome FROM appserver_project_routes WHERE route_id=?",
                                           (route_id,)).fetchone())

    async def test_unlabelled_final_cannot_complete_the_remaining_sibling(self):
        audit, game = await self.pair()
        finish(self.inbox, game, "codex:" + PROJECT_OWNER, "Ready to play")
        await self.native(*final("Ready to play"))
        self.assertEqual(self.job(audit), ("accepted", None))
        self.assertEqual(self.job(game), ("completed", "Ready to play"))

    async def test_recovery_uses_the_original_matched_body_after_an_interruption(self):
        audit, game = await self.pair()
        router = self.adapter.router
        chosen = final(f"ROUTE_RESULT {game}\nReady to play")[-1]["params"]["turn"]
        altered = final(f"ROUTE_RESULT {audit}\nDifferent answer")[-1]["params"]["turn"]
        with patch.object(router, "_close", side_effect=RuntimeError("interrupted before closing the route")):
            with self.assertRaises(RuntimeError):
                await router._complete_turn("codex:" + PROJECT_OWNER, OWNER_TURN, chosen)
        await router._complete_turn("codex:" + PROJECT_OWNER, OWNER_TURN, altered)
        self.assertEqual(self.job(audit), ("accepted", None))
        self.assertEqual(self.job(game), ("completed", "Ready to play"))

    async def test_a_quoted_label_is_not_an_owner_decision(self):
        audit, game = await self.pair()
        await self.native(*final(f"The request contained the following text.\nROUTE_RESULT {game}\nEnd of quote."))
        self.assertEqual(self.job(audit), ("accepted", None))
        self.assertEqual(self.job(game), ("accepted", None))

    async def test_early_terminal_waits_for_a_sibling_acceptance(self):
        audit, game = await self.pair()
        db = self.inbox.db
        with db:
            db.execute("UPDATE appserver_project_routes SET state='sending',turn_id=NULL WHERE route_id=?", (game,))
        terminal = final("Ready to play")[-1]["params"]["turn"]
        router = self.adapter.router
        await router._complete_turn("codex:" + PROJECT_OWNER, OWNER_TURN, terminal)
        self.assertEqual(self.job(audit), ("accepted", None))
        client = db.execute("SELECT client_id FROM appserver_project_routes WHERE route_id=?", (game,)).fetchone()[0]
        router.acknowledged(client, OWNER_TURN)
        await router._complete_turn("codex:" + PROJECT_OWNER, OWNER_TURN, terminal)
        self.assertEqual(self.job(game), ("accepted", None))
        self.assertEqual(self.job(audit), ("accepted", None))

    async def test_shared_runtime_error_stays_with_chief(self):
        audit, game = await self.pair()
        terminal = {"id": OWNER_TURN, "status": "failed", "error": {"message": "Could not finish the game task."}}
        await self.adapter.router._complete_turn("codex:" + PROJECT_OWNER, OWNER_TURN, terminal)
        self.assertEqual(self.job(audit), ("accepted", None))
        self.assertEqual(self.job(game), ("accepted", None))
