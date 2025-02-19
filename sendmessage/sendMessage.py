from redbot.core import commands, checks
import discord


class SendMessageCog(commands.Cog):
    """A cog to send messages on behalf of the bot."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="send")
    @checks.admin_or_permissions(
        manage_guild=True
    )  # Restrict to admins or users with manage_guild permission
    async def send_message(self, ctx, channel: discord.TextChannel, *, message: str):
        """
        Sends a message to a specified channel on behalf of the bot.
        Usage: [p]send <channel_id> <message>
        """
        try:
            await channel.send(message)
            await ctx.send(f"Message sent to {channel.mention}!")
        except Exception as e:
            await ctx.send(f"Failed to send message: {e}")

    @send_message.error
    async def send_message_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You don't have permission to use this command.")
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.send(
                "Channel not found. Please provide a valid channel ID or mention."
            )
        else:
            await ctx.send(f"An error occurred: {error}")
