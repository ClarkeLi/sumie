using HarmonyLib;

namespace DrawGuessBridge
{
    /// <summary>
    /// Offline Sketchbook sessions have no PlayFab user, but the game's real fill
    /// tool dereferences LocalPlayfabId. Supply a stable local owner only in that case.
    /// Online sessions keep the original getter untouched.
    /// </summary>
    [HarmonyPatch(typeof(DrawModule), "get_LocalPlayfabId")]
    internal static class LocalPlayerFallbackPatch
    {
        [HarmonyPrefix]
        private static bool Prefix(ref string __result)
        {
            try
            {
                if (NetworkInit.PlayfabUser == null)
                {
                    __result = "local-bridge";
                    return false;
                }
            }
            catch
            {
                // Fall through to the original getter.
            }

            return true;
        }
    }
}
