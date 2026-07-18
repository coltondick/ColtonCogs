from .goldaccess import GoldAccess


async def setup(bot):
    await bot.add_cog(GoldAccess(bot))
