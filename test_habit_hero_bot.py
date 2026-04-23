import unittest

from habit_hero_bot import (
    TelegramUser,
    add_habit,
    clear_habit_reminder,
    complete_habit,
    due_reminder_habits,
    get_user_row,
    init_db,
    level_for_xp,
    list_active_habits,
    open_db,
    parse_reminder_time,
    reward_for_streak,
    set_habit_reminder,
)


class HabitHeroTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        init_db(self.db)
        self.user = TelegramUser(
            chat_id=1,
            user_id=2,
            username="tester",
            first_name="Test",
        )

    def tearDown(self) -> None:
        self.db.close()

    def test_add_habit_deduplicates_active_names(self) -> None:
        first, created_first = add_habit(self.db, self.user, "Drink water")
        second, created_second = add_habit(self.db, self.user, "drink water")

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(list_active_habits(self.db, self.user)), 1)

    def test_completion_awards_xp_and_blocks_same_day_duplicate(self) -> None:
        habit, _ = add_habit(self.db, self.user, "Read 10 pages")
        first = complete_habit(self.db, self.user, habit, date_key="2026-04-18")
        duplicate_habit = list_active_habits(self.db, self.user)[0]
        duplicate = complete_habit(self.db, self.user, duplicate_habit, date_key="2026-04-18")
        user_row = get_user_row(self.db, self.user)

        self.assertFalse(first.already_done)
        self.assertTrue(duplicate.already_done)
        self.assertEqual(first.streak, 1)
        self.assertEqual(first.reward.total_xp, 11)
        self.assertEqual(int(user_row["xp"]), 11)
        self.assertEqual(int(user_row["total_checkins"]), 1)

    def test_streak_increases_on_consecutive_days(self) -> None:
        habit, _ = add_habit(self.db, self.user, "Walk")
        day_one = complete_habit(self.db, self.user, habit, date_key="2026-04-18")
        day_one_habit = day_one.habit
        day_two = complete_habit(self.db, self.user, day_one_habit, date_key="2026-04-19")

        self.assertEqual(day_two.streak, 2)
        self.assertEqual(day_two.reward.total_xp, 12)

    def test_streak_resets_after_gap(self) -> None:
        habit, _ = add_habit(self.db, self.user, "Stretch")
        day_one = complete_habit(self.db, self.user, habit, date_key="2026-04-18")
        day_three = complete_habit(self.db, self.user, day_one.habit, date_key="2026-04-20")

        self.assertEqual(day_three.streak, 1)

    def test_rewards_and_levels_are_predictable(self) -> None:
        self.assertEqual(reward_for_streak(1).total_xp, 11)
        self.assertEqual(reward_for_streak(3).total_xp, 18)
        self.assertEqual(reward_for_streak(7).total_xp, 32)
        self.assertEqual(level_for_xp(0), 1)
        self.assertEqual(level_for_xp(100), 2)

    def test_reminder_time_parsing(self) -> None:
        self.assertEqual(parse_reminder_time("7:05"), "07:05")
        self.assertEqual(parse_reminder_time("21:30"), "21:30")
        with self.assertRaises(ValueError):
            parse_reminder_time("25:00")

    def test_set_and_clear_reminder(self) -> None:
        habit, _ = add_habit(self.db, self.user, "Meditate")
        updated, reminder_time = set_habit_reminder(self.db, self.user, habit.id, "7:05")
        loaded = list_active_habits(self.db, self.user)[0]

        self.assertTrue(updated)
        self.assertEqual(reminder_time, "07:05")
        self.assertEqual(loaded.reminder_time, "07:05")

        self.assertTrue(clear_habit_reminder(self.db, self.user, habit.id))
        loaded = list_active_habits(self.db, self.user)[0]
        self.assertIsNone(loaded.reminder_time)

    def test_due_reminders_skip_completed_habits(self) -> None:
        habit, _ = add_habit(self.db, self.user, "Journal")
        self.db.execute(
            "UPDATE habits SET reminder_time = '09:00', last_reminded_date = NULL WHERE id = ?",
            (habit.id,),
        )
        self.db.commit()

        due = due_reminder_habits(self.db, "2026-04-18", "09:00")
        self.assertEqual([item.id for item in due], [habit.id])

        complete_habit(self.db, self.user, habit, date_key="2026-04-18")
        due = due_reminder_habits(self.db, "2026-04-18", "09:30")
        self.assertEqual(due, [])


if __name__ == "__main__":
    unittest.main()
