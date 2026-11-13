; Inno Setup : installateur Windows de Gold AI Signal Bot
;   iscc /DMyAppVersion=2.0.0 packaging\installer.iss
#ifndef MyAppVersion
  #define MyAppVersion "2.0.0"
#endif
#define MyAppName "Gold AI Signal Bot"
#define MyAppExe "Gold-AI-Signal-Bot.exe"
#define MyAppPublisher "ck526438-lgtm"

[Setup]
AppId={{AB490605-32D7-4109-BCD3-B0F2EF5F02ED}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Installation sans droits administrateur (dans le profil de l'utilisateur) ;
; l'utilisateur peut choisir « pour tous les utilisateurs » dans l'assistant.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Gold-AI-Signal-Bot-Setup-{#MyAppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
LicenseFile=license_fr.txt

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"
Name: "autostart"; Description: "Lancer automatiquement au démarrage de Windows"; GroupDescription: "Démarrage :"; Flags: unchecked

[Files]
Source: "..\dist\Gold-AI-Signal-Bot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\Guide_Gold_AI_Signal_Bot.pdf"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{localappdata}\{#MyAppName}"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"
Name: "{group}\Guide d'utilisation"; Filename: "{app}\Guide_Gold_AI_Signal_Bot.pdf"
Name: "{group}\Dossier des données (réglages, journal)"; Filename: "{localappdata}\{#MyAppName}"
Name: "{group}\Mode terminal (sans navigateur)"; Filename: "{app}\{#MyAppExe}"; Parameters: "watch"; WorkingDir: "{app}"
Name: "{group}\Désinstaller {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "--no-browser"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Lancer {#MyAppName} maintenant"; Flags: nowait postinstall skipifsilent

[Messages]
fr.FinishedLabel=L'installation est terminée.%n%nAu premier lancement, un assistant détecte automatiquement MetaTrader 5, le symbole de l'or (XAUUSD, GOLD…) de ton courtier et le décalage horaire.%n%nLe robot de trading est désactivé par défaut : active-le dans l'onglet Robot, en Simulateur d'abord.

[Code]
function MT5Found(): Boolean;
var
  FindRec: TFindRec;
begin
  Result := DirExists(ExpandConstant('{userappdata}\MetaQuotes\Terminal'));
  if not Result and FindFirst(ExpandConstant('{commonpf64}\*'), FindRec) then
  begin
    try
      repeat
        if FileExists(ExpandConstant('{commonpf64}\') + FindRec.Name + '\terminal64.exe') then
          Result := True;
      until Result or not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WizardSilent() and not MT5Found() then
    MsgBox('MetaTrader 5 n''a pas été trouvé sur cet ordinateur.' + #13#10 + #13#10 +
           'L''application fonctionne sans lui (prix Yahoo Finance ou mode Démo), mais il est nécessaire ' +
           'pour les prix de ton courtier et pour le robot de trading.' + #13#10 + #13#10 +
           'Télécharge MetaTrader 5 depuis le site de ton courtier, connecte-toi, puis relance l''assistant ' +
           'depuis Réglages → Assistant de configuration.', mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\{#MyAppName}');
    if DirExists(DataDir) and not UninstallSilent() then
      if MsgBox('Supprimer aussi tes réglages, ton journal de signaux et l''historique du robot ?' + #13#10 + #13#10 +
                'Choisis « Non » pour les garder (recommandé si tu réinstalles plus tard).',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
